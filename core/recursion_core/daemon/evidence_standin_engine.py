#!/usr/bin/python3 -I
"""STAND-IN ENGINE for the daemon evidence harness (``evidence_e2e.py``). Not Claude Code.

It speaks the stream-json lines ``routes.claude_code.ClaudeCodeAdapter`` reads (``system``,
``assistant``, ``result``) so the real daemon, launcher and adapter can be driven end to end
with no subscription, no API key, no model and no network. Every line it prints carries
``"stand_in_engine": true``. The harness installs a copy named ``claude`` because the adapter
launches only a binary with that name.

Behaviour: read stdin to EOF (the one user message), start one helper process that ignores
SIGINT (so a Stop has to end the tree, not just the engine), then print one ``assistant`` line
every half second. ``ticks=N`` in the prompt ends the run with a ``result`` line after N ticks;
without it the engine runs until it is stopped. SIGINT prints a ``stand_in_sigint`` line and exits.
"""
import json
import os
import re
import signal
import subprocess
import sys
import time

LABEL = 'stand-in engine: not Claude Code; no model, no network, no credential'


def emit(**fields):
    sys.stdout.write(json.dumps({'stand_in_engine': True, **fields}) + '\n')
    sys.stdout.flush()


def on_sigint(*_):
    emit(type='system', subtype='stand_in_sigint', note='SIGINT received: the daemon told the engine to stop')
    os._exit(130)


def main():
    signal.signal(signal.SIGINT, on_sigint)
    # Names only: shows which variables the child-environment policy let through, never a value.
    emit(type='system', subtype='init', note=LABEL, argv=sys.argv[1:], env_names=sorted(os.environ),
         pid=os.getpid(), home=os.environ.get('HOME'))
    prompt = ''
    for line in sys.stdin:
        try:
            message = json.loads(line)
            prompt = message['message']['content'][0]['text']
        except (ValueError, KeyError, IndexError, TypeError):
            emit(type='system', subtype='stand_in_bad_input')
    emit(type='system', subtype='stand_in_stdin_eof', note='stdin closed; not treated as cancellation')
    helper = subprocess.Popen(
        [sys.executable, '-I', '-c',
         'import signal, time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(3600)'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    emit(type='system', subtype='stand_in_helper', helper_pid=helper.pid,
         note='helper ignores SIGINT and sits in its own session: only a tree kill ends it')
    match = re.search(r'ticks=(\d+)', prompt)
    ticks = int(match.group(1)) if match else None
    count = 0
    while ticks is None or count < ticks:
        count += 1
        emit(type='assistant', message={'content': [{'type': 'text', 'text': f'stand-in engine tick {count}'}]})
        time.sleep(0.5)
    helper.kill()
    helper.wait()
    emit(type='result', subtype='success', is_error=False, result=f'stand-in engine finished after {count} ticks')


if __name__ == '__main__':
    main()
