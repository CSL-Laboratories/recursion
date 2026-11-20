"""The one-shot argv, the prompt on standard input and the parser, end to end, against a FAKE ``hermes``.

``fake_hermes.py`` replays a synthetic stream; no real Hermes is run. Run by module name:

    PYTHONPATH=core python -m unittest recursion_core.routes.engines.fixtures.hermes.test_hermes_fake
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from recursion_core.routes.engines import hermes

HERE = Path(__file__).resolve().parent
FAKE = HERE / 'fake_hermes.py'
CONNECTION = 'api_key:hermes-test'


def launch(args: list[str], prompt: str, stream: str = 's02-tools.jsonl', exit_status: int = 0):
    """Run the fake as the launcher would run ``hermes``: these args, the prompt written to stdin, stdin closed."""
    with tempfile.TemporaryDirectory() as scratch:
        seen = Path(scratch) / 'seen.json'
        env = dict(os.environ, FAKE_HERMES_STREAM=str(HERE / 'streams' / stream),
                   FAKE_HERMES_EXIT=str(exit_status), FAKE_HERMES_SEEN=str(seen))
        done = subprocess.run([sys.executable, '-B', str(FAKE), *args], input=prompt.encode('utf-8'),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False)
        return done, json.loads(seen.read_text(encoding='utf-8'))


def drafts_of(done: subprocess.CompletedProcess) -> tuple[hermes.HermesStreamParser, list[dict]]:
    parser = hermes.HermesStreamParser(CONNECTION)
    drafts = []
    for chunk in done.stdout.splitlines(keepends=True):
        drafts.extend(parser.parse(chunk.decode('utf-8')))
    for chunk in done.stderr.splitlines(keepends=True):
        drafts.extend(parser.parse_stderr(chunk.decode('utf-8')))
    drafts.extend(parser.finish(done.returncode))
    return parser, drafts


class FakeHermesTest(unittest.TestCase):
    def test_the_prompt_goes_on_stdin_and_the_stream_parses_to_one_ok_result(self):
        prompt = 'list the files; $(rm -rf /) is text, not a command\n'
        argv = hermes.one_shot_argv()
        self.assertEqual(argv[0], hermes.DEFAULT_EXECUTABLE)
        done, seen = launch(argv[1:], prompt)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(seen, {'argv': list(hermes.ONE_SHOT_ARGS), 'prompt': prompt})
        self.assertNotIn(prompt.strip(), ' '.join(argv))
        parser, drafts = drafts_of(done)
        self.assertEqual([d['payload'] for d in drafts if d['type'] == 'result'], [{'code': None, 'detail': 'done'}])
        self.assertEqual([d['payload']['call_id'] for d in drafts if d['type'] == 'tool.result'], ['call-b', 'call-a'])
        self.assertIs(parser.exit_agrees(done.returncode), True)
        stderr = [d['payload'] for d in drafts if d['type'] == 'engine.output' and d['payload']['stream'] == 'stderr']
        self.assertEqual(''.join(p['text'] for p in stderr), done.stderr.decode('utf-8'))

    def test_the_fake_accepts_only_the_lanes_argv_so_a_prompt_on_the_command_line_is_refused(self):
        # Upstream accepts ``-q <prompt>``; the fake refuses it to hold the lane's own argv in place.
        done, seen = launch(['chat', '-q', 'hello', '--format', 'stream-json'], 'hello')
        self.assertEqual(done.returncode, 2)
        self.assertEqual(seen['prompt'], '', 'stdin was not read')
        parser, drafts = drafts_of(done)
        self.assertEqual([d['payload'] for d in drafts if d['type'] == 'result'], [{
            'code': 'engine_error',
            'detail': 'hermes exit 2: Hermes refused the invocation; hermes said: fake hermes: unexpected arguments'}])
        self.assertIs(parser.exit_agrees(2), True)

    def test_an_exit_status_that_differs_from_the_record_is_seen(self):
        done, _ = launch(list(hermes.ONE_SHOT_ARGS), 'hi', stream='s01-text-only.jsonl', exit_status=1)
        parser, drafts = drafts_of(done)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(parser.exit_code, 0)
        self.assertIs(parser.exit_agrees(done.returncode), False)


if __name__ == '__main__':
    unittest.main()
