# Model: Claude Opus 5.5 (claude-opus-5-5), lane SESSIONS.
"""The turn state machine: the only code that changes a session record.

Every function takes a session.v1 record and returns a NEW record, with ``revision`` one higher, or raises
``Refused`` and changes nothing. None of them reads or writes anything: the caller loads the record in its
transaction, asks the machine, and saves what comes back (``store.save``) in the same transaction.

THE SESSION, as session.v1 (#368) freezes it:

    idle --open_turn--> live --turn_finished or turn_interrupted--> idle
     |                    |
     |                    +--request_stop--> stop_pending --stop_settled(stopped|ended)--> idle
     +--request_stop (only with a worker) ---^      |
                                                    +-- stop_settled(pending): stays stop_pending

- At most one live unit per session: a turn opens only from ``idle``. While ``live`` or ``stop_pending`` a new
  turn is ``busy``; on an ``ended`` session it is ``conflict`` (``session_ended``).
- ``stop_pending`` is a Stop that is not confirmed. Nothing returns the session to ``idle`` but a Stop that
  answered ``stopped`` or ``ended``: a turn that finishes while a Stop is pending ends, and the session stays
  ``stop_pending`` with its worker. A ``pending`` answer changes nothing.
- A turn is ``queued`` from the commit that dispatches it until the engine's own typed acceptance of its input
  arrives (``turn_running``); then ``running``; then one of ``ended``, ``failed``, ``interrupted`` or
  ``stopped``, which are final. A turn whose run was stopped before the engine took its input never started:
  it is ``failed`` with the code ``stopped_before_start``, since session.v1 gives a ``stopped`` turn a start
  time.
- ``worker`` is the session's unit and what the daemon last knew of it. A turn opens with the run it will use
  already bound (its id is allocated before the dispatch).
"""
from __future__ import annotations

import copy

from recursion_core.sessions import records


class Refused(Exception):
    """The transition does not apply to this record. ``code`` is a typed word; nothing was changed."""

    def __init__(self, code: str, detail: str = '') -> None:
        super().__init__(f'{code}: {detail}' if detail else code)
        self.code, self.detail = code, detail


def _next(session: dict) -> dict:
    record = copy.deepcopy(session)
    record['revision'] = session['revision'] + 1
    return record


def _turn(record: dict, turn_id) -> dict:
    turn = next((turn for turn in record['turns'] if turn['turn_id'] == turn_id), None)
    if turn is None:
        raise Refused('turn_unknown', str(turn_id))
    return turn


def new_session(*, session_id: str, created_at: str, tenant: str, principal: dict, route: dict, workspace,
                budget: dict) -> dict:
    """A root session with no turn and no unit, at revision 1."""
    record = {'session_id': session_id, 'created_at': created_at, 'tenant': tenant, 'principal': dict(principal),
              'parent_session_id': None, 'spawn_kind': 'root', 'state': 'idle',
              'workspace': copy.deepcopy(workspace), 'route': copy.deepcopy(route), 'engine_session_id': None,
              'worker': None, 'active_turn_id': None, 'turns': [], 'budget': dict(budget), 'revision': 1}
    records.check_session_kept(record)
    return record


def turn_refusal(session: dict):
    """Why a new turn cannot open now, as ``(code, detail)``, or None when it can."""
    state = session['state']
    if state == 'ended':
        return 'conflict', 'session_ended'
    if state in ('live', 'stop_pending'):
        return 'busy', state
    return None


def open_turn(session: dict, *, turn_id: str, run_id: str, worker_alive: bool) -> dict:
    """A new turn, dispatched to ``run_id``: queued until the engine takes its input. The session goes ``live``."""
    refusal = turn_refusal(session)
    if refusal is not None:
        raise Refused(*refusal)
    record = _next(session)
    record['turns'].append({'turn_id': turn_id, 'index': len(session['turns']), 'run_id': run_id,
                            'state': 'queued', 'started_at': None, 'ended_at': None, 'outcome': None,
                            'result': None, 'expires_at': None})
    record.update(state='live', active_turn_id=turn_id, worker={'run_id': run_id, 'alive': bool(worker_alive)})
    records.check_session_kept(record)
    return record


def turn_running(session: dict, turn_id: str, at: str) -> dict:
    """The engine took the turn's input (a typed acceptance, how new_turn): the turn is running."""
    turn = _turn(session, turn_id)
    if turn['state'] != 'queued':
        raise Refused('turn_not_queued', turn['state'])
    record = _next(session)
    turn = _turn(record, turn_id)
    turn.update(state='running', started_at=at)
    if record['worker'] is not None and record['worker']['run_id'] == turn['run_id']:
        record['worker']['alive'] = True
    records.check_session_kept(record)
    return record


def turn_finished(session: dict, turn_id: str, *, outcome: str, result: dict | None, at: str,
                  worker_alive: bool) -> dict:
    """The turn came to its end. ``outcome`` 'failed' needs a result with a code; the others of ``ENDED_OUTCOMES``
    a result with none. A queued turn can only fail (it never started). The session goes ``idle``, keeping its
    worker when the worker is still alive, unless a Stop is pending: then it stays ``stop_pending``."""
    turn = _turn(session, turn_id)
    if turn['state'] not in ('queued', 'running'):
        raise Refused('turn_final', turn['state'])
    if outcome == 'failed':
        if not (isinstance(result, dict) and isinstance(result.get('code'), str)):
            raise Refused('result', 'a failed turn has a code')
        state = 'failed'
    elif outcome in records.ENDED_OUTCOMES:
        if turn['state'] != 'running' or not (isinstance(result, dict) and result.get('code') is None):
            raise Refused('result', 'an ended turn ran and has a result with no code')
        state = 'ended'
    else:
        raise Refused('outcome', str(outcome))
    record = _next(session)
    turn = _turn(record, turn_id)
    turn.update(state=state, ended_at=at, outcome=outcome, result=copy.deepcopy(result))
    if record['active_turn_id'] == turn_id:
        record['active_turn_id'] = None
    if record['state'] == 'live':
        record['state'] = 'idle'
        alive = bool(worker_alive) and record['worker'] is not None and record['worker']['run_id'] == turn['run_id']
        record['worker'] = {'run_id': turn['run_id'], 'alive': True} if alive else None
    elif record['state'] == 'stop_pending' and record['worker'] is not None:
        record['worker']['alive'] = bool(worker_alive)
    records.check_session_kept(record)
    return record


def turn_interrupted(session: dict, turn_id: str, at: str, *, worker_alive: bool) -> dict:
    """The driver interrupted the running turn (its own cancel): the turn is ``interrupted``, final, with no
    result. Only a running turn can be interrupted this way (session.v1 gives an interrupted turn a start time).
    The session goes ``idle`` keeping a worker that is still alive, or stays ``stop_pending`` if a Stop is pending."""
    turn = _turn(session, turn_id)
    if turn['state'] != 'running':
        raise Refused('turn_not_running', turn['state'])
    record = _next(session)
    turn = _turn(record, turn_id)
    turn.update(state='interrupted', ended_at=at, outcome='interrupted', result=None)
    if record['active_turn_id'] == turn_id:
        record['active_turn_id'] = None
    if record['state'] == 'live':
        record['state'] = 'idle'
        alive = bool(worker_alive) and record['worker'] is not None and record['worker']['run_id'] == turn['run_id']
        record['worker'] = {'run_id': turn['run_id'], 'alive': True} if alive else None
    elif record['state'] == 'stop_pending' and record['worker'] is not None:
        record['worker']['alive'] = bool(worker_alive)
    records.check_session_kept(record)
    return record


def request_stop(session: dict) -> dict:
    """A Stop of the session's unit was asked: ``stop_pending`` until it is confirmed. Only a session with a unit
    can be stopping; asking again while pending changes nothing (``stop_already_pending``)."""
    if session['state'] == 'stop_pending':
        raise Refused('stop_already_pending')
    if session['state'] == 'ended':
        raise Refused('conflict', 'session_ended')
    if session['worker'] is None:
        raise Refused('no_unit')
    record = _next(session)
    record['state'] = 'stop_pending'
    records.check_session_kept(record)
    return record


def stop_settled(session: dict, code: str, at: str) -> dict:
    """What the Stop answered. 'stopped' (the removal was attested) and 'ended' (the run had already ended) settle
    it: the open turn, if any, is final and the session is ``idle`` with no unit. 'pending' settles nothing and is
    refused (``stop_not_confirmed``), so the caller keeps the session as it is. Any other word is not a Stop's
    answer and is refused too: a client that meets a code it does not know treats the run as NOT stopped."""
    if session['state'] != 'stop_pending':
        raise Refused('no_stop_pending', session['state'])
    if code not in ('stopped', 'ended'):
        raise Refused('stop_not_confirmed', str(code))
    record = _next(session)
    active = record['active_turn_id']
    if active is not None:
        turn = _turn(record, active)
        if turn['started_at'] is None:
            turn.update(state='failed', ended_at=at, outcome='failed',
                        result={'code': 'stopped_before_start', 'detail': '', 'result_ref': None})
        else:
            turn.update(state='stopped', ended_at=at, outcome='stopped', result=None)
    record.update(state='idle', active_turn_id=None, worker=None)
    records.check_session_kept(record)
    return record


def engine_session(session: dict, engine_session_id: str) -> dict:
    """The engine reported its own id for the conversation (untrusted text, kept to resume it). Set once."""
    if not (isinstance(engine_session_id, str) and 1 <= len(engine_session_id) <= 256):
        raise Refused('engine_session_id')
    if session['engine_session_id'] == engine_session_id:
        raise Refused('unchanged')
    if session['engine_session_id'] is not None:
        raise Refused('engine_session_id_changed')
    record = _next(session)
    record['engine_session_id'] = engine_session_id
    records.check_session_kept(record)
    return record
