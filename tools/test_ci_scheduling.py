"""Execute the actual job conditions across events and dependency outcomes."""
from __future__ import annotations

import itertools
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]


def condition(job: str) -> str:
    text = (ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8')
    match = re.search(rf'^  {job}:\n(.*?)(?=^  \w[\w-]*:|\Z)', text, re.M | re.S)
    assert match
    expression = re.search(r'^    if: >\n((?:      .*\n)+)', match[1], re.M)
    assert expression
    return ' '.join(line.strip() for line in expression[1].splitlines())


def evaluate(expression, *, event, action='', draft=False, full=False,
             settle='skipped', linux='success', run_full='true', cancelled=False):
    expression = expression.replace('github.event.pull_request.labels.*.name', 'labels')
    expression = expression.replace('&&', ' and ').replace('||', ' or ')
    expression = re.sub(r'!(?!=)', ' not ', expression)
    return bool(eval(expression, {'__builtins__': {}}, {
        'github': NS(event_name=event, event=NS(action=action, pull_request=NS(draft=draft))),
        'needs': NS(settle=NS(result=settle), linux=NS(result=linux, outputs=NS(run_full=run_full))),
        'labels': ['ci:full'] if full else [], 'false': False, 'true': True,
        'always': lambda: True, 'cancelled': lambda: cancelled,
        'contains': lambda values, item: item in values,
    }))


class SchedulingTests(unittest.TestCase):
    def test_settle_runs_only_for_ready_synchronize(self):
        for event, action, draft in itertools.product(
                ['pull_request', 'push', 'workflow_dispatch', 'merge_group'],
                ['opened', 'reopened', 'ready_for_review', 'synchronize', ''], [True, False]):
            self.assertEqual(evaluate(condition('settle'), event=event, action=action, draft=draft),
                             event == 'pull_request' and action == 'synchronize' and not draft)

    def test_actual_conditions_event_result_matrix(self):
        outcomes = ['success', 'failure', 'cancelled', 'skipped', '', 'pending']
        events = [('pull_request', action, draft) for action in
                  ['opened', 'reopened', 'ready_for_review', 'synchronize'] for draft in [True, False]]
        events += [(e, '', False) for e in ['push', 'workflow_dispatch', 'merge_group']]
        queue_enabled = 'merge_group:' in (ROOT / '.github/workflows/ci.yml').read_text()
        for job in ['linux-tests', 'windows', 'macos']:
            expression = condition(job)
            self.assertIn('always()', expression)
            self.assertIn('!cancelled()', expression)
            for (event, action, draft), settle, linux, full, cancelled in itertools.product(
                    events, outcomes, outcomes, [False, True], [False, True]):
                expected_settle = event == 'pull_request' and action == 'synchronize' and not draft
                expected = not cancelled and (settle == 'success' or
                           (settle == 'skipped' and not expected_settle))
                if job != 'linux-tests':
                    selected = event == 'workflow_dispatch' or (event == 'pull_request' and full)
                    selected |= event == 'merge_group' and queue_enabled
                    selected |= job == 'windows' and event == 'push'
                    expected &= linux == 'success' and selected
                args = dict(event=event, action=action, draft=draft, full=full,
                            settle=settle, linux=linux, cancelled=cancelled)
                self.assertEqual(evaluate(expression, **args), expected, (job, args))
            if job != 'linux-tests':
                self.assertFalse(evaluate(expression, event='workflow_dispatch', run_full='false'))

    @unittest.skipIf(os.name == 'nt' or shutil.which('bash') is None, 'POSIX shell runner')
    def test_required_linux_runs_actual_shell_for_every_dependency_result(self):
        text = (ROOT / '.github/workflows/ci.yml').read_text()
        job = text.split('  linux:\n', 1)[1].split('  windows:\n', 1)[0]
        self.assertIn('needs: [settle, linux-tests]', job)
        self.assertIn('if: always()', job)
        self.assertIn('runs-on: ubuntu-latest', job)
        self.assertIn('if: cancelled()\n        run: exit 1', job)
        import textwrap
        shell = textwrap.dedent(job.split('        run: |\n', 1)[1]).strip()
        states = ['success', 'failure', 'cancelled', 'skipped', 'pending', '']
        for settle, linux, wait in itertools.product(states, states, ['true', 'false']):
            env = dict(os.environ, SETTLE_RESULT=settle, LINUX_RESULT=linux, WAIT_EXPECTED=wait)
            result = subprocess.run(['bash', '-e', '-c', shell], env=env, capture_output=True)
            expected = linux == 'success' and settle == ('success' if wait == 'true' else 'skipped')
            self.assertEqual(result.returncode == 0, expected, (settle, linux, wait))


if __name__ == '__main__':
    unittest.main()
