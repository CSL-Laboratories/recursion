#!/usr/bin/env python3
"""A fake ``hermes`` for the gateway-ownership tests. It is not Hermes and runs nothing of it.

Two shapes:

* ``gateway run --no-supervise --external-supervisor --profile <name>`` stays in the foreground
  (no new session, no second process). It records a live turn, and on SIGINT or SIGTERM it marks
  that turn ended and exits. A file ``ignore-int`` in ``HERMES_HOME`` makes it ignore SIGINT, so
  only a later SIGKILL ends it.
* ``chat --query-file - --format stream-json`` is the viewer. It exits 3. When ``RECURSION_E9_PARK``
  is ``1`` it first parks a turn in a new session, outside the caller's process group, and leaves
  that process running. That is the client-only shape: the viewer is gone and the turn is not.

Anything else exits 2. ``RECURSION_E9_GATEWAY_TURN`` is the turn record, a JSON object.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time

GATEWAY = ['gateway', 'run', '--no-supervise', '--external-supervisor']
VIEWER = ['chat', '--query-file', '-', '--format', 'stream-json']


def _write(path: str, obj: dict) -> None:
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(obj, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _serve(argv: list[str]) -> int:
    turn = os.environ['RECURSION_E9_GATEWAY_TURN']
    home = os.environ.get('HERMES_HOME', '')
    ended = False

    def _end(signum, _frame) -> None:
        nonlocal ended
        ended = True

    if os.path.isfile(os.path.join(home, 'ignore-int')):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    else:
        signal.signal(signal.SIGINT, _end)
    signal.signal(signal.SIGTERM, _end)
    _write(turn, {'alive': True, 'pid': os.getpid(), 'pgrp': os.getpgrp(), 'argv': argv, 'home': home})
    while not ended:
        try:
            time.sleep(0.05)
        except InterruptedError:
            continue
    _write(turn, {'alive': False, 'pid': os.getpid(), 'pgrp': os.getpgrp(), 'argv': argv, 'home': home})
    return 0


def _park(turn: str, argv: list[str]) -> None:
    """Leave one process in a new session and return only after its turn record is visible."""
    pid = os.fork()
    if pid == 0:
        os.setsid()
        child = os.fork()
        if child:
            os._exit(0)
        _write(turn, {'alive': True, 'pid': os.getpid(), 'pgrp': os.getpgrp(), 'argv': argv,
                      'home': os.environ.get('HERMES_HOME', '')})
        while True:
            time.sleep(30)
    os.waitpid(pid, 0)
    for _ in range(100):
        if os.path.exists(turn):
            return
        time.sleep(0.02)
    sys.exit(1)


def main(argv: list[str]) -> int:
    if argv[:4] == GATEWAY and len(argv) == 6 and argv[4] == '--profile' and argv[5]:
        return _serve(argv)
    if argv == VIEWER:
        try:
            sys.stdin.read()
        except Exception:
            pass
        if os.environ.get('RECURSION_E9_PARK') == '1':
            _park(os.environ['RECURSION_E9_GATEWAY_TURN'], argv)
        return 3
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
