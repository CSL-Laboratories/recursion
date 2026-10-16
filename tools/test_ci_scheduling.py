"""Execute the actual job conditions across events and dependency outcomes."""
from __future__ import annotations

import itertools
import re
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
    def test_actual_conditions_event_result_matrix(self):
        outcomes = ['success', 'failure', 'cancelled', 'skipped', '', 'pending']
        events = [('pull_request', action, draft) for action in
                  ['opened', 'reopened', 'ready_for_review', 'synchronize'] for draft in [True, False]]
        events += [(e, '', False) for e in ['push', 'workflow_dispatch', 'merge_group']]
        queue_enabled = 'merge_group:' in (ROOT / '.github/workflows/ci.yml').read_text()
        for job in ['linux', 'windows', 'macos']:
            expression = condition(job)
            self.assertIn('always()', expression)
            self.assertIn('!cancelled()', expression)
            for (event, action, draft), settle, linux, full, cancelled in itertools.product(
                    events, outcomes, outcomes, [False, True], [False, True]):
                expected_settle = event == 'pull_request' and action == 'synchronize' and not draft
                expected = not cancelled and (settle == 'success' or
                           (settle == 'skipped' and not expected_settle))
                if job != 'linux':
                    selected = event == 'workflow_dispatch' or (event == 'pull_request' and full)
                    selected |= event == 'merge_group' and queue_enabled
                    selected |= job == 'windows' and event == 'push'
                    expected &= linux == 'success' and selected
                args = dict(event=event, action=action, draft=draft, full=full,
                            settle=settle, linux=linux, cancelled=cancelled)
                self.assertEqual(evaluate(expression, **args), expected, (job, args))
            if job != 'linux':
                self.assertFalse(evaluate(expression, event='workflow_dispatch', run_full='false'))


if __name__ == '__main__':
    unittest.main()
