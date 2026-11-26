# Model: Claude Opus 5.5 (claude-opus-5-5), lane SESSIONS.
"""The command family of lane SESSIONS for ``recursion-control`` (seam S2): one top-level command, ``session``.

    recursion-control session start  --tenant T [--engine E] [--prompt TEXT]
    recursion-control session turn   SESSION_ID --text TEXT --revision N
    recursion-control session queue  SESSION_ID --text TEXT
    recursion-control session steer  SESSION_ID --text TEXT --turn TURN_ID
    recursion-control session update ITEM_ID --revision N [--text TEXT] [--delivery next_turn|steer]
    recursion-control session cancel ITEM_ID --revision N
    recursion-control session status SESSION_ID
    recursion-control session list   [--tenant T] [--state S] [--limit N] [--after SESSION_ID]

Each keyed subcommand takes ``--command-id`` to send a retry under the id of the first attempt; without it the
command mints one (a random part made once per process, then a counter: methods.v1, ``command_id``) and writes
it to the error stream, so a retry can name it. The answer is printed as JSON on the output stream, unchanged.
The exit status is the CLI's own: 0 for an answer whose code is ``ok``, 1 for any other answer.

``register(table)`` hands the family to a ``CommandTable``. NOTHING CALLS IT IN THIS SLICE: the lane registers no
command until the seams merge (its start note), and each subcommand's method row must be implemented by a
running daemon first. The tests call it on a table of their own.
"""
from __future__ import annotations

import json
import secrets
import threading

NAMES = ('session',)
DELIVERIES = ('next_turn', 'steer')
STATES = ('idle', 'live', 'stop_pending', 'ended')

_PROCESS_PART = secrets.token_hex(8)
_counter = 0
_counter_lock = threading.Lock()


def mint_command_id() -> str:
    global _counter
    with _counter_lock:
        _counter += 1
        return f'cli-{_PROCESS_PART}-{_counter}'


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise ValueError(text)
    return value


def parser(family) -> None:
    top = family.add_parser('session', help='sessions: turns, a queue of messages, at most one live unit (beta)')
    sub = top.add_subparsers(dest='session_command', required=True)

    def keyed(name, help_text):
        found = sub.add_parser(name, help=help_text)
        found.add_argument('--command-id', dest='command_id', help='the id of an earlier attempt, to retry it')
        return found

    found = keyed('start', 'start a session, with its first turn if --prompt is given')
    found.add_argument('--tenant', required=True)
    found.add_argument('--engine')
    found.add_argument('--prompt')
    found = keyed('turn', 'send the next turn of an idle session')
    found.add_argument('session_id')
    found.add_argument('--text', required=True)
    found.add_argument('--revision', type=_positive, required=True, help='the session revision you read')
    found = keyed('queue', 'queue a message for the next turn')
    found.add_argument('session_id')
    found.add_argument('--text', required=True)
    found = keyed('steer', 'queue a message for the turn in progress')
    found.add_argument('session_id')
    found.add_argument('--text', required=True)
    found.add_argument('--turn', required=True, help='the turn in progress you read')
    found = keyed('update', 'change a queued message')
    found.add_argument('item_id')
    found.add_argument('--revision', type=_positive, required=True)
    found.add_argument('--text')
    found.add_argument('--delivery', choices=DELIVERIES)
    found = keyed('cancel', 'withdraw a queued message')
    found.add_argument('item_id')
    found.add_argument('--revision', type=_positive, required=True)
    found = sub.add_parser('status', help='a session and its queue')
    found.add_argument('session_id')
    found = sub.add_parser('list', help='sessions, in id order')
    found.add_argument('--tenant')
    found.add_argument('--state', choices=STATES)
    found.add_argument('--limit', type=_positive)
    found.add_argument('--after')


def request_of(args) -> tuple:
    """``(method, params)`` for a parsed ``session`` subcommand. Only what the user gave is sent."""
    command = args.session_command
    if command == 'status':
        return 'session.status', {'session_id': args.session_id}
    if command == 'list':
        params = {key: getattr(args, key) for key in ('tenant', 'state', 'limit', 'after')
                  if getattr(args, key) is not None}
        return 'session.list', params
    params = {'command_id': args.command_id or None}
    if command == 'start':
        params.update(tenant=args.tenant)
        for key in ('engine', 'prompt'):
            if getattr(args, key) is not None:
                params[key] = getattr(args, key)
        return 'session.start', params
    if command == 'turn':
        params.update(session_id=args.session_id, text=args.text, expected_revision=args.revision)
        return 'session.turn', params
    if command == 'queue':
        params.update(session_id=args.session_id, text=args.text)
        return 'session.queue', params
    if command == 'steer':
        params.update(session_id=args.session_id, text=args.text, expected_turn_id=args.turn)
        return 'session.steer', params
    if command == 'update':
        params.update(item_id=args.item_id, expected_revision=args.revision)
        for key in ('text', 'delivery'):
            if getattr(args, key) is not None:
                params[key] = getattr(args, key)
        return 'queue.update', params
    if command == 'cancel':
        params.update(item_id=args.item_id, expected_revision=args.revision)
        return 'queue.cancel', params
    raise ValueError(f'no session subcommand {command!r}')


def dispatch(args, client) -> int:
    method, params = request_of(args)
    if 'command_id' in params and params['command_id'] is None:
        params['command_id'] = mint_command_id()
        print(f'command_id: {params["command_id"]}', file=args.err)
    answer = client.call(method, params)
    print(json.dumps(answer, sort_keys=True), file=args.out)
    return 0 if isinstance(answer, dict) and answer.get('code') == 'ok' else 1


def register(table) -> None:
    """Hand the family to ``table`` (a ``CommandTable``). Called by nobody in this slice but the tests."""
    table.register(NAMES, parser, dispatch)
