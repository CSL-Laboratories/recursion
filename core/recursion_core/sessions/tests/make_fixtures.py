# Model: Claude Opus 5.5 (claude-opus-5-5), lane SESSIONS.
"""The lane's fixtures: records this lane's own code writes, taken at fixed points of one scenario.

Run as a module to write them (``python -m recursion_core.sessions.tests.make_fixtures``); ``test_fixtures``
makes them again on every run and checks that the files are exactly what the code writes, and, on an
interpreter with ``jsonschema``, that each validates against the schema the index names (SPINE's, #368). So a
fixture here is evidence of what the lane commits, not a hand-written example. They are the fixtures this lane
sends SPINE for what it produces: session.v1, queue.item.v1, the private delivery binding, and the payloads of
the events it appends.

Ids and times are the tests' fixed ones (``support.Minter``, ``support.Clock``), not real ones.
"""
from __future__ import annotations

import json
import sys

from recursion_core.sessions import store
from recursion_core.sessions.tests.support import FIXTURES, Lane, UnitRefused, stop_answer

MODEL = 'Claude Opus 5.5 (claude-opus-5-5), lane SESSIONS'


def _session(lane, session_id):
    with lane.store.transaction() as tx:
        return store.load(tx, session_id)


def _item(lane, item_id):
    with lane.store.transaction() as tx:
        return store.load_item(tx, item_id)


def _binding(lane, item_id):
    with lane.store.transaction() as tx:
        row = next(row for row in store.deliveries(tx) if row['item_id'] == item_id)
    return {key: row[key] for key in ('delivery_id', 'item_id', 'session_id', 'run_id', 'worker_incarnation',
                                      'target_turn_id', 'how')}


def scenario() -> list:
    """``[(file, schema, shows, record)]``, in the order the index lists them."""
    out = []

    def keep(file, schema, shows, record):
        out.append((file, schema, shows, record))

    lane = Lane()
    idle = lane.start(engine='claude_code', command_id='cmd-fixture-001')['session']
    keep('session.v1/s01-a-new-session-is-idle.json', 'session.v1.json',
         'session.start with no prompt: a root session, idle, with no turn and no unit, at revision 1.', idle)

    worker = lane.start(engine='codex', prompt='Read the README.', command_id='cmd-fixture-002')['session']
    keep('session.v1/s02-a-turn-dispatched-and-not-yet-taken.json', 'session.v1.json',
         'session.start with a prompt: the turn is committed queued with its run already bound, and the session is '
         'live with a worker the daemon has not yet seen alive. Nothing has been written to an engine.', worker)
    sid = worker['session_id']
    lane.settle()
    lane.accept_input(sid)
    running = _session(lane, sid)
    keep('session.v1/s03-the-engine-took-the-input.json', 'session.v1.json',
         'The engine\'s typed acceptance of the input arrived: the turn is running and the worker is alive.', running)

    steer = lane.call('session.steer', {'command_id': 'cmd-fixture-003', 'session_id': sid, 'text': 'Skip the docs.',
                                        'expected_turn_id': running['active_turn_id']})['item']
    keep('queue.item.v1/q01-a-steer-queued.json', 'queue.item.v1.json',
         'session.steer during a running turn: queued, first in the order, with no delivery yet.', steer)
    lane.settle()
    keep('queue.item.v1/q02-a-steer-delivering-into-the-turn-in-progress.json', 'queue.item.v1.json',
         'The steer\'s delivery began: delivering, with its delivery id, written into the turn in progress.',
         _item(lane, steer['item_id']))
    keep('queue.item.v1/b01-the-binding-of-a-mid-turn-steer.json', 'queue.item.v1.json#/$defs/delivery_binding',
         'The private binding committed with the steer\'s delivering, before the write: aimed at the turn in progress.',
         _binding(lane, steer['item_id']))
    lane.accept_input(sid)
    keep('queue.item.v1/q03-a-steer-delivered-into-the-turn-in-progress.json', 'queue.item.v1.json',
         'The engine took the steer mid-turn: delivered, carrying the turn it went into.',
         _item(lane, steer['item_id']))

    later = lane.call('session.queue', {'command_id': 'cmd-fixture-004', 'session_id': sid,
                                        'text': 'Then update the changelog.'})['item']
    edited = lane.call('queue.update', {'command_id': 'cmd-fixture-005', 'item_id': later['item_id'],
                                        'expected_revision': 1, 'text': 'Then update the changelog and the version.'})
    keep('queue.item.v1/q04-a-message-for-the-next-turn-edited-once.json', 'queue.item.v1.json',
         'session.queue, then queue.update while it is still queued: its revision rose to 2.', edited['item'])
    turn_id = running['active_turn_id']
    lane.sessions.turn_finished(sid, turn_id, outcome='waiting', result={'code': None, 'detail': 'Done for now.',
                                                                         'result_ref': None}, worker_alive=True)
    keep('session.v1/s04-idle-with-a-worker-waiting.json', 'session.v1.json',
         'The engine yielded and waits for the user; the worker that takes another message is still alive.',
         _session(lane, sid))
    lane.settle()
    keep('queue.item.v1/q05-a-message-delivering-as-the-next-turn.json', 'queue.item.v1.json',
         'At the turn\'s end the message began the next turn: delivering, with the new turn allocated in its binding.',
         _item(lane, later['item_id']))
    keep('queue.item.v1/b02-the-binding-of-a-next-turn-message.json', 'queue.item.v1.json#/$defs/delivery_binding',
         'The binding of a message that begins a new turn: the turn id was allocated before the dispatch.',
         _binding(lane, later['item_id']))
    lane.accept_input(sid)
    keep('queue.item.v1/q06-a-message-delivered-as-a-new-turn.json', 'queue.item.v1.json',
         'The engine took it as a new turn: delivered, carrying the turn it began.', _item(lane, later['item_id']))

    lane.units.stop_answers = [stop_answer(_session(lane, sid)['worker']['run_id'], 'pending')]
    lane.sessions.request_stop(sid)
    lane.sessions.run_pending()
    keep('session.v1/s05-a-stop-pending.json', 'session.v1.json',
         'A Stop of the session\'s unit was asked and answered pending: stop_pending, the turn still open, the worker '
         'kept. No new turn starts while it lasts.', _session(lane, sid))
    lane.sessions.run_pending()
    keep('session.v1/s06-idle-after-the-stop-with-its-turn-stopped.json', 'session.v1.json',
         'The Stop was confirmed: the turn in progress is stopped and the session is idle with no unit.',
         _session(lane, sid))

    gone = lane.call('session.queue', {'command_id': 'cmd-fixture-006', 'session_id': sid,
                                       'text': 'Never mind.'})['item']
    cancelled = lane.call('queue.cancel', {'command_id': 'cmd-fixture-007', 'item_id': gone['item_id'],
                                           'expected_revision': 1})['item']
    keep('queue.item.v1/q07-cancelled.json', 'queue.item.v1.json',
         'queue.cancel while it was still queued: cancelled, never delivered.', cancelled)

    lane.units.then = [UnitRefused('engine_missing', 'no such binary')]
    failed = lane.start(engine='claude_code', prompt='Read the README.', command_id='cmd-fixture-008')['session']
    lane.settle()
    keep('session.v1/s07-a-turn-whose-unit-could-not-start.json', 'session.v1.json',
         'The launch definitely did not start: the turn failed with the refusal\'s code and the session is idle.',
         _session(lane, failed['session_id']))

    resumer = lane.start(engine='opencode', command_id='cmd-fixture-009')['session']
    waiting = lane.call('session.queue', {'command_id': 'cmd-fixture-010', 'session_id': resumer['session_id'],
                                          'text': 'Read the README.'})['item']
    lane.sessions.dispatch_queued()
    after = lane.restarted()
    keep('queue.item.v1/q08-outcome-unknown-after-a-restart.json', 'queue.item.v1.json',
         'The daemon stopped after delivering was committed and before anything was written: at the restart its '
         'outcome is unknown, and it is never sent again.', _item(after, waiting['item_id']))

    by_type = {}
    for event in after.store.events:
        by_type.setdefault(event['type'], event)
    for kind in ('steer.queued', 'steer.sent', 'input.accepted', 'turn.started', 'turn.ended',
                 'effect.outcome_unknown'):
        event = by_type[kind]
        keep(f'event.v1/e-{kind.replace(".", "-")}.json', f'event.v1.json#/$defs/{kind}',
             f'The payload of the first {kind} this scenario appended (filed under session {event["session_id"]}, '
             f'source {event["source"]}, trust {event["trust"]}).', event['payload'])
    return out


def text_of(record) -> str:
    return json.dumps(record, indent=2, ensure_ascii=True) + '\n'


def index_of(found: list) -> dict:
    return {'model': MODEL,
            'about': 'Records lane SESSIONS writes, made by make_fixtures.py from one scenario; each validates '
                     'against the schema named (SPINE\'s, #368).',
            'fixtures': [{'file': file, 'schema': schema, 'shows': shows} for file, schema, shows, _ in found]}


def main() -> int:
    found = scenario()
    for file, _, _, record in found:
        path = FIXTURES / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text_of(record), encoding='utf-8', newline='\n')
    (FIXTURES / 'index.json').write_text(text_of(index_of(found)), encoding='utf-8', newline='\n')
    print(f'{len(found)} fixtures written under {FIXTURES}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
