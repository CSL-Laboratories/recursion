"""A FAKE ``hermes`` for tests. It is not Hermes and runs nothing of it.

Called with exactly the one-shot arguments (``chat --query-file - --format stream-json``), it reads the
prompt from standard input, replays the synthetic stream named by ``FAKE_HERMES_STREAM`` byte for byte on
stdout, writes Hermes's ``session_id:`` line on stderr and exits with ``FAKE_HERMES_EXIT`` (default 0).
Called with anything else, it refuses, without reading stdin. That is this fake's rule, not upstream's:
it accepts only the argv this lane chose, and upstream Hermes also accepts ``-q <prompt>`` with
``--format stream-json``. The refusal is shaped like Hermes's own failed start in stream-json mode: an
``init`` line, a failed ``result`` line with exit code 2, and exit 2.
When ``FAKE_HERMES_SEEN`` names a file, it writes there the arguments it got and the prompt it read.
"""
from __future__ import annotations

import json
import os
import sys
import time

ONE_SHOT = ['chat', '--query-file', '-', '--format', 'stream-json']


def _line(obj: dict) -> None:
    sys.stdout.write(json.dumps({**obj, 'timestamp': int(time.time() * 1000)}, ensure_ascii=False) + '\n')


def _refuse(message: str) -> int:
    _line({'type': 'system', 'subtype': 'init', 'model': '', 'session_id': ''})
    _line({'type': 'result', 'session_id': '', 'exit_code': 2, 'text': '',
           'tokens': {'input': 0, 'output': 0, 'total': 0, 'cache_read': 0, 'cache_write': 0},
           'duration_ms': 0, 'error': message})
    sys.stdout.flush()
    print(f'Error: {message}', file=sys.stderr)
    return 2


def main(argv: list[str]) -> int:
    prompt = sys.stdin.read() if argv == ONE_SHOT else ''
    seen = os.environ.get('FAKE_HERMES_SEEN')
    if seen:
        with open(seen, 'w', encoding='utf-8') as handle:
            json.dump({'argv': argv, 'prompt': prompt}, handle)
    if argv != ONE_SHOT:
        return _refuse('fake hermes: unexpected arguments')
    if not prompt.strip():
        return _refuse('--query-file - is empty')
    with open(os.environ['FAKE_HERMES_STREAM'], 'rb') as handle:
        sys.stdout.buffer.write(handle.read())
    sys.stdout.flush()
    print('\nsession_id: 20261114_170000_a1b2c3', file=sys.stderr)
    return int(os.environ.get('FAKE_HERMES_EXIT', '0'))


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
