"""A non-string stand-in prompt is bad input, not a crash.

Run from the repository root:
PYTHONPATH=core python -m unittest recursion_core.daemon.tests.test_evidence_standin_engine
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

STANDIN = Path(__file__).resolve().parents[1] / 'evidence_standin_engine.py'
CHILD_FLAGS = ['-X', 'gil=0', '-B'] if sys._xoptions.get('gil') == '0' else ['-X', 'utf8', '-B']


class StandInPromptTests(unittest.TestCase):
    def test_non_string_standin_prompt_must_be_reported_as_bad_input(self):
        result = subprocess.run(
            [sys.executable, *CHILD_FLAGS, str(STANDIN)],
            input=json.dumps({'message': {'content': [{'text': 7}]}}) + '\n',
            capture_output=True, text=True, timeout=2)
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertNotIn('Traceback', result.stderr)
        self.assertTrue(any(line.get('subtype') == 'stand_in_bad_input' for line in lines))
        self.assertEqual(result.returncode, 2)

    def test_deeply_nested_line_is_bad_input_not_a_crash(self):
        # A few thousand brackets are ordinary bad JSON. One hundred thousand
        # raises RecursionError on both interpreters used here.
        depth = 100000
        result = subprocess.run(
            [sys.executable, *CHILD_FLAGS, str(STANDIN)],
            input='[' * depth + '\n',
            capture_output=True, text=True, timeout=5)
        lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        self.assertNotIn('Traceback', result.stderr)
        self.assertTrue(any(line.get('subtype') == 'stand_in_bad_input' for line in lines))
        self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main()
