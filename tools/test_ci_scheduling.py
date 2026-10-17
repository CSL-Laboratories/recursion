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


def value(expression, *, event, action='', draft=False, full=False, label='', number=217,
          settle='skipped', linux='success', run_full='true', cancelled=False, attempt=1):
    expression = expression.replace('github.event.pull_request.labels.*.name', 'labels')
    expression = expression.replace('&&', ' and ').replace('||', ' or ')
    expression = re.sub(r'!(?!=)', ' not ', expression)
    return eval(expression, {'__builtins__': {}}, {
        'github': NS(run_attempt=attempt, event_name=event, ref='refs/heads/master', workflow='CI',
                     event=NS(action=action, label=NS(name=label),
                              pull_request=NS(draft=draft, number=number))),
        'needs': NS(settle=NS(result=settle), linux=NS(result=linux, outputs=NS(run_full=run_full))),
        'labels': ['ci:full'] if full else [], 'false': False, 'true': True,
        'inputs': NS(candidate='bound-candidate' if full else ''),
        'always': lambda: True, 'cancelled': lambda: cancelled,
        'contains': lambda values, item: item in values,
        'format': lambda pattern, *args: pattern.format(*args),
    })


def evaluate(expression, **kwargs):
    return bool(value(expression, **kwargs))


class SchedulingTests(unittest.TestCase):
    def test_settle_runs_only_for_ready_synchronize(self):
        for event, action, draft in itertools.product(
                ['pull_request', 'push', 'workflow_dispatch', 'merge_group'],
                ['opened', 'reopened', 'ready_for_review', 'synchronize', ''], [True, False]):
            self.assertEqual(evaluate(condition('settle'), event=event, action=action, draft=draft),
                             event == 'pull_request' and action == 'synchronize' and not draft)

    def test_actual_conditions_event_result_matrix(self):
        outcomes = ['success', 'failure', 'cancelled', 'skipped', '', 'pending']
        events = [('pull_request', action, draft, '') for action in
                  ['opened', 'reopened', 'ready_for_review', 'synchronize'] for draft in [True, False]]
        events += [('pull_request', 'labeled', draft, label)
                   for draft, label in itertools.product([True, False], ['ci:full', 'bug', ''])]
        events += [(e, '', False, '') for e in ['push', 'workflow_dispatch', 'merge_group', 'pull_request_review']]
        queue_enabled = 'merge_group:' in (ROOT / '.github/workflows/ci.yml').read_text()
        for job in ['linux-tests', 'windows', 'macos']:
            expression = condition(job)
            self.assertIn('always()', expression)
            self.assertIn('!cancelled()', expression)
            for (event, action, draft, label), settle, linux, full, cancelled in itertools.product(
                    events, outcomes, outcomes, [False, True], [False, True]):
                expected_settle = event == 'pull_request' and action == 'synchronize' and not draft
                expected = not cancelled and (settle == 'success' or
                           (settle == 'skipped' and not expected_settle))
                expected &= action != 'labeled'
                if job != 'linux-tests':
                    selected = event == 'workflow_dispatch' or (event == 'pull_request_review' and full)
                    selected |= event == 'merge_group' and queue_enabled
                    selected |= job == 'windows' and event == 'push'
                    expected &= linux == 'success' and selected
                args = dict(event=event, action=action, draft=draft, full=full, label=label,
                            settle=settle, linux=linux, cancelled=cancelled)
                self.assertEqual(evaluate(expression, **args), expected, (job, args))
            if job != 'linux-tests':
                self.assertFalse(evaluate(expression, event='workflow_dispatch', run_full='false'))

    def test_candidate_partial_reruns_never_allocate_paid_jobs(self):
        for job, attempt in itertools.product(['linux-tests', 'windows', 'macos'], [2, 3, 99]):
            self.assertFalse(evaluate(condition(job), event='pull_request_review', full=True,
                                      attempt=attempt))

    def test_label_routing_names_concurrency_and_required_jobs(self):
        text = (ROOT / '.github/workflows/ci.yml').read_text()
        self.assertIn('ready_for_review, labeled]', text)
        self.assertFalse((ROOT / '.github/workflows/ci-full-label.yml').exists())
        self.assertFalse((ROOT / '.github/workflows/ci-full-matrix.yml').exists())
        group_line = next(line for line in text.splitlines() if line.strip().startswith('group:'))
        group_expr = re.findall(r'\$\{\{ (.*?) \}\}', group_line)[-1]
        base = dict(event='pull_request', action='synchronize')
        ordinary_group = value(group_expr, **base)
        full_group = value(group_expr, event='pull_request', action='labeled', label='ci:full')
        self.assertNotEqual(ordinary_group, full_group)
        for label, draft in itertools.product(['ci:full', 'bug', ''], [False, True]):
            args = dict(event='pull_request', action='labeled', label=label, draft=draft)
            eligible = False  # Label presence/events no longer authorize paid CI.
            self.assertEqual(value(group_expr, **args) == ordinary_group, eligible)
            for job in ['settle', 'linux-tests', 'linux', 'windows', 'macos', 'platform-gates']:
                body = re.search(rf'^  {job}:\n(.*?)(?=^  [\w-]+:|\Z)', text, re.M | re.S)[1]
                name_expr = re.search(r'^    name: \$\{\{ (.*?) \}\}', body, re.M)[1]
                self.assertEqual(value(name_expr, **args), job if eligible else 'ignored-label-' + job)
            for job in ['linux', 'platform-gates']:
                # Required aggregators still run on failed dependencies and cancellation;
                # ignored labels instantiate only distinct skipped checks.
                for result, cancelled in itertools.product(['failure', 'skipped', 'pending'], [False, True]):
                    self.assertEqual(evaluate(condition(job), linux=result, cancelled=cancelled, **args), eligible)
        # Manual and other-PR lanes remain independent; label text never enters a shell.
        self.assertNotEqual(value(group_expr, event='workflow_dispatch', number=0), ordinary_group)
        self.assertNotEqual(value(group_expr, event='pull_request', number=218), ordinary_group)
        for line in text.splitlines():
            if line.lstrip().startswith('run:'):
                self.assertNotIn('github.event.label', line)

    @unittest.skipIf(os.name == 'nt' or shutil.which('bash') is None, 'POSIX shell runner')
    def test_required_linux_runs_actual_shell_for_every_dependency_result(self):
        text = (ROOT / '.github/workflows/ci.yml').read_text()
        job = text.split('  linux:\n', 1)[1].split('  windows:\n', 1)[0]
        self.assertIn('needs: [settle, linux-tests]', job)
        self.assertIn('always()', condition('linux'))
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
