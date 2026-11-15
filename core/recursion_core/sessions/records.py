# Model: Claude Opus 5.5 (claude-opus-5-5), lane SESSIONS.
"""Ids, times, and the checks every record of this lane passes before it is written.

The records are SPINE's, published in #368: ``session.v1`` (a session and its turns), ``queue.item.v1``
(one queued message) and the private ``delivery_binding`` beside a queue item. The checks here state the
same rules as those schemas, by hand, because the daemon's interpreter has no JSON Schema library and
nothing under ``core/`` may import one. They are run by the store before EVERY write: a bug in this lane
cannot commit a record the schema refuses. The tests run these checks over #368's own good and bad
fixtures, and on an interpreter that has ``jsonschema`` they validate this lane's records against the
schemas themselves.

A check raises ``RecordError(path, rule)``; it never repairs a record.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone

# The id patterns of common.v1.json (#368). A pattern is matched whole.
SESSION_ID = re.compile(r'ses-[A-Za-z0-9_-]{1,60}')
TURN_ID = re.compile(r'turn-[A-Za-z0-9_-]{1,59}')
ITEM_ID = re.compile(r'qi-[A-Za-z0-9_-]{1,61}')
DELIVERY_ID = re.compile(r'dlv-[A-Za-z0-9_-]{1,60}')
RUN_ID = re.compile(r'[A-Za-z0-9_-]{1,64}')
TOKEN = re.compile(r'[A-Za-z0-9._:-]{1,128}')
COMMAND_ID = re.compile(r'[A-Za-z0-9._:-]{8,128}')
CODE_WORD = re.compile(r'[a-z][a-z0-9_]{0,63}')
ENGINE_NAME = re.compile(r'[a-z][a-z0-9_]{0,31}')
TIMESTAMP = re.compile(r'[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]'
                       r'\.[0-9]{3}Z')

SESSION_STATES = ('idle', 'live', 'stop_pending', 'ended')
SPAWN_KINDS = ('root', 'fork', 'child')
TURN_STATES = ('queued', 'running', 'interrupted', 'ended', 'failed', 'stopped')
TURN_OUTCOMES = ('candidate_done', 'done', 'waiting', 'paused', 'interrupted', 'failed', 'stopped')
ENDED_OUTCOMES = ('candidate_done', 'done', 'waiting', 'paused')
PRINCIPAL_KINDS = ('owner', 'device', 'session', 'plugin', 'internal')
CONNECTION_KINDS = ('subscription', 'api_key', 'passthrough')
ITEM_STATES = ('queued', 'delivering', 'delivered', 'cancelled', 'outcome_unknown')
ITEM_FINAL = ('delivered', 'cancelled', 'outcome_unknown')
DELIVERIES = ('next_turn', 'steer')
HOWS = ('new_turn', 'mid_turn')
# What a binding's 'how' may be for each delivery an item asks for (queue.item.v1; conformance/rules.py HOW_OF).
HOW_OF = {'next_turn': ('new_turn',), 'steer': ('mid_turn', 'new_turn')}

SESSION_KEYS = ('session_id', 'created_at', 'tenant', 'principal', 'parent_session_id', 'spawn_kind', 'state',
                'workspace', 'route', 'engine_session_id', 'worker', 'active_turn_id', 'turns', 'budget', 'revision')
TURN_KEYS = ('turn_id', 'index', 'run_id', 'state', 'started_at', 'ended_at', 'outcome', 'result', 'expires_at')
ITEM_KEYS = ('item_id', 'session_id', 'order', 'text', 'revision', 'delivery', 'state', 'delivery_id', 'turn_id',
             'created_at')
BINDING_KEYS = ('delivery_id', 'item_id', 'session_id', 'run_id', 'worker_incarnation', 'target_turn_id', 'how')


class RecordError(ValueError):
    """A record breaks a rule of its schema. ``path`` is where (``/turns/2/state``), ``rule`` what."""

    def __init__(self, path: str, rule: str) -> None:
        super().__init__(f'{path or "/"}: {rule}')
        self.path, self.rule = path, rule


# ---------------------------------------------------------------------------------------------- ids, times
def mint(prefix: str) -> str:
    """A fresh id: the prefix and 128 random bits as 32 hex digits (``ses-``, ``turn-``, ``qi-``, ``dlv-``,
    ``run-``, ``op-``). Every pattern above admits it, and the run id has the control API's own form."""
    return prefix + secrets.token_hex(16)


def timestamp(moment: datetime | None = None) -> str:
    """A moment as the records write it: UTC, three decimals, a 'Z' (common.v1.json, timestamp)."""
    moment = (moment or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return moment.strftime('%Y-%m-%dT%H:%M:%S.') + f'{moment.microsecond // 1000:03d}Z'


# ------------------------------------------------------------------------------------------- the pieces
def _is_text(value) -> bool:
    return isinstance(value, str)


def _no_newline(value: str) -> bool:
    return '\r' not in value and '\n' not in value


def _matches(pattern, value) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None and _no_newline(value)


def _need(ok: bool, path: str, rule: str) -> None:
    if not ok:
        raise RecordError(path, rule)


def _exact_keys(value, keys, path: str) -> None:
    _need(isinstance(value, dict), path, 'object')
    missing = [key for key in keys if key not in value]
    _need(not missing, f'{path}/{missing[0]}' if missing else path, 'required')
    extra = sorted(set(value) - set(keys))
    _need(not extra, f'{path}/{extra[0]}' if extra else path, 'additionalProperties')


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value \
        and value not in (float('inf'), float('-inf'))


def _instant(value) -> bool:
    """A timestamp that is a real moment (the schemas' ``x-instant``): February has no 31st."""
    if not _matches(TIMESTAMP, value):
        return False
    try:
        datetime.strptime(value[:19], '%Y-%m-%dT%H:%M:%S')
    except ValueError:
        return False
    return True


def _nullable(check, value) -> bool:
    return value is None or check(value)


def check_principal(value, path: str = '/principal') -> None:
    _exact_keys(value, ('kind', 'id'), path)
    _need(value['kind'] in PRINCIPAL_KINDS, f'{path}/kind', 'enum')
    _need(_matches(TOKEN, value['id']), f'{path}/id', 'pattern')


def check_route(value, path: str = '/route') -> None:
    _exact_keys(value, ('engine', 'model', 'connection'), path)
    _need(_matches(ENGINE_NAME, value['engine']), f'{path}/engine', 'pattern')
    model = value['model']
    _need(model is None or (_is_text(model) and len(model) >= 1), f'{path}/model', 'type')
    connection = value['connection']
    _exact_keys(connection, ('kind', 'id'), f'{path}/connection')
    _need(connection['kind'] in CONNECTION_KINDS, f'{path}/connection/kind', 'enum')
    label = connection['id']
    _need(_is_text(label) and 1 <= len(label) <= 200 and _no_newline(label), f'{path}/connection/id', 'type')


def check_workspace(value, path: str = '/workspace') -> None:
    if value is None:
        return
    _exact_keys(value, ('path', 'mode', 'base_ref', 'worktree_path'), path)
    _need(_is_text(value['path']) and len(value['path']) >= 1, f'{path}/path', 'type')
    _need(value['mode'] in ('direct', 'worktree'), f'{path}/mode', 'enum')
    for key in ('base_ref', 'worktree_path'):
        _need(_nullable(lambda text: _is_text(text) and len(text) >= 1, value[key]), f'{path}/{key}', 'type')
    if value['mode'] == 'worktree':
        _need(_is_text(value['base_ref']), f'{path}/base_ref', 'type')
        _need(_is_text(value['worktree_path']), f'{path}/worktree_path', 'type')
    else:
        _need(value['worktree_path'] is None, f'{path}/worktree_path', 'type')


def check_budget(value, path: str = '/budget') -> None:
    _exact_keys(value, ('cap_usd',), path)
    _need(_is_number(value['cap_usd']) and value['cap_usd'] >= 0, f'{path}/cap_usd', 'minimum')


# --------------------------------------------------------------------------------------------- a turn
def check_turn(turn, path: str) -> None:
    _exact_keys(turn, TURN_KEYS, path)
    _need(_matches(TURN_ID, turn['turn_id']), f'{path}/turn_id', 'pattern')
    _need(_is_int(turn['index']) and turn['index'] >= 0, f'{path}/index', 'minimum')
    _need(_nullable(lambda run: _matches(RUN_ID, run), turn['run_id']), f'{path}/run_id', 'pattern')
    state = turn['state']
    _need(state in TURN_STATES, f'{path}/state', 'enum')
    for key in ('started_at', 'ended_at', 'expires_at'):
        _need(_nullable(_instant, turn[key]), f'{path}/{key}', 'timestamp')
    _need(turn['outcome'] is None or turn['outcome'] in TURN_OUTCOMES, f'{path}/outcome', 'enum')
    result = turn['result']
    if result is not None:
        _exact_keys(result, ('code', 'detail', 'result_ref'), f'{path}/result')
        _need(_nullable(lambda code: _matches(CODE_WORD, code), result['code']), f'{path}/result/code', 'pattern')
        _need(_is_text(result['detail']), f'{path}/result/detail', 'type')
        _need(_nullable(lambda ref: _matches(TOKEN, ref), result['result_ref']), f'{path}/result/result_ref',
              'pattern')
    # The rules of session.v1 $defs/turn, one per state.
    if state == 'queued':
        for key in ('started_at', 'ended_at', 'outcome', 'result'):
            _need(turn[key] is None, f'{path}/{key}', 'queued')
    elif state == 'running':
        _need(turn['run_id'] is not None, f'{path}/run_id', 'running')
        _need(turn['started_at'] is not None, f'{path}/started_at', 'running')
        for key in ('ended_at', 'outcome', 'result'):
            _need(turn[key] is None, f'{path}/{key}', 'running')
    elif state == 'ended':
        for key in ('run_id', 'started_at', 'ended_at'):
            _need(turn[key] is not None, f'{path}/{key}', 'ended')
        _need(turn['outcome'] in ENDED_OUTCOMES, f'{path}/outcome', 'ended')
        _need(isinstance(result, dict), f'{path}/result', 'ended')
        _need(result['code'] is None, f'{path}/result/code', 'ended')
    elif state in ('interrupted', 'stopped'):
        for key in ('run_id', 'started_at', 'ended_at'):
            _need(turn[key] is not None, f'{path}/{key}', state)
        _need(turn['outcome'] == state, f'{path}/outcome', state)
    elif state == 'failed':
        _need(turn['ended_at'] is not None, f'{path}/ended_at', 'failed')
        _need(turn['outcome'] == 'failed', f'{path}/outcome', 'failed')
        _need(isinstance(result, dict), f'{path}/result', 'failed')
        _need(isinstance(result['code'], str), f'{path}/result/code', 'failed')


# ------------------------------------------------------------------------------------------- a session
def check_session(record) -> None:
    """Raise ``RecordError`` unless ``record`` is a session.v1 record (#368, draft 3)."""
    _exact_keys(record, SESSION_KEYS, '')
    _need(_matches(SESSION_ID, record['session_id']), '/session_id', 'pattern')
    _need(_instant(record['created_at']), '/created_at', 'timestamp')
    _need(_is_text(record['tenant']) and len(record['tenant']) >= 1, '/tenant', 'minLength')
    check_principal(record['principal'])
    _need(record['spawn_kind'] in SPAWN_KINDS, '/spawn_kind', 'enum')
    parent = record['parent_session_id']
    _need(parent is None or _matches(SESSION_ID, parent), '/parent_session_id', 'pattern')
    if record['spawn_kind'] == 'root':
        _need(parent is None, '/parent_session_id', 'root')
    else:
        _need(isinstance(parent, str), '/parent_session_id', 'not_root')
    state = record['state']
    _need(state in SESSION_STATES, '/state', 'enum')
    check_workspace(record['workspace'])
    check_route(record['route'])
    engine_session = record['engine_session_id']
    _need(engine_session is None or (_is_text(engine_session) and 1 <= len(engine_session) <= 256),
          '/engine_session_id', 'type')
    worker = record['worker']
    if worker is not None:
        _exact_keys(worker, ('run_id', 'alive'), '/worker')
        _need(_matches(RUN_ID, worker['run_id']), '/worker/run_id', 'pattern')
        _need(isinstance(worker['alive'], bool), '/worker/alive', 'type')
    active = record['active_turn_id']
    _need(active is None or _matches(TURN_ID, active), '/active_turn_id', 'pattern')
    turns = record['turns']
    _need(isinstance(turns, list), '/turns', 'type')
    for number, turn in enumerate(turns):
        check_turn(turn, f'/turns/{number}')
    check_budget(record['budget'])
    _need(_is_int(record['revision']) and record['revision'] >= 1, '/revision', 'minimum')
    # The rules of session.v1, one per state.
    if state == 'idle':
        _need(active is None, '/active_turn_id', 'idle')
    elif state == 'live':
        _need(isinstance(active, str), '/active_turn_id', 'live')
        _need(isinstance(worker, dict), '/worker', 'live')
    elif state == 'stop_pending':
        _need(isinstance(worker, dict), '/worker', 'stop_pending')
    elif state == 'ended':
        _need(active is None, '/active_turn_id', 'ended')
        _need(worker is None, '/worker', 'ended')


def check_session_kept(record) -> None:
    """``check_session`` and the rules of this lane that the schema does not state: turn indexes are 0, 1, 2 in
    order with no turn id twice, and an active turn is one of the session's turns and is still queued or
    running. Run before every write."""
    check_session(record)
    turns = record['turns']
    for number, turn in enumerate(turns):
        _need(turn['index'] == number, f'/turns/{number}/index', 'in_order')
    ids = [turn['turn_id'] for turn in turns]
    _need(len(set(ids)) == len(ids), '/turns', 'unique_turn_id')
    active = record['active_turn_id']
    if active is not None:
        turn = next((turn for turn in turns if turn['turn_id'] == active), None)
        _need(turn is not None, '/active_turn_id', 'a_turn_of_the_session')
        _need(turn['state'] in ('queued', 'running'), '/active_turn_id', 'not_final')
    for number, turn in enumerate(turns):
        if turn['state'] in ('queued', 'running'):
            _need(turn['turn_id'] == active, f'/turns/{number}/state', 'only_the_active_turn_is_open')


# ----------------------------------------------------------------------------------------- a queue item
def check_item(item) -> None:
    """Raise ``RecordError`` unless ``item`` is a queue.item.v1 record (#368, draft 7)."""
    _exact_keys(item, ITEM_KEYS, '')
    _need(_matches(ITEM_ID, item['item_id']), '/item_id', 'pattern')
    _need(_matches(SESSION_ID, item['session_id']), '/session_id', 'pattern')
    _need(_is_int(item['order']) and item['order'] >= 1, '/order', 'minimum')
    _need(_is_text(item['text']) and len(item['text']) >= 1, '/text', 'minLength')
    _need(_is_int(item['revision']) and item['revision'] >= 1, '/revision', 'minimum')
    _need(item['delivery'] in DELIVERIES, '/delivery', 'enum')
    state = item['state']
    _need(state in ITEM_STATES, '/state', 'enum')
    _need(_nullable(lambda value: _matches(DELIVERY_ID, value), item['delivery_id']), '/delivery_id', 'pattern')
    _need(_nullable(lambda value: _matches(TURN_ID, value), item['turn_id']), '/turn_id', 'pattern')
    _need(_instant(item['created_at']), '/created_at', 'timestamp')
    if state in ('queued', 'cancelled'):
        _need(item['delivery_id'] is None, '/delivery_id', state)
        _need(item['turn_id'] is None, '/turn_id', state)
    elif state in ('delivering', 'outcome_unknown'):
        _need(isinstance(item['delivery_id'], str), '/delivery_id', state)
        _need(item['turn_id'] is None, '/turn_id', state)
    elif state == 'delivered':
        _need(isinstance(item['delivery_id'], str), '/delivery_id', state)
        _need(isinstance(item['turn_id'], str), '/turn_id', state)


def check_binding(binding) -> None:
    """Raise ``RecordError`` unless ``binding`` is a delivery binding (queue.item.v1, $defs/delivery_binding)."""
    _exact_keys(binding, BINDING_KEYS, '')
    for key, pattern in (('delivery_id', DELIVERY_ID), ('item_id', ITEM_ID), ('session_id', SESSION_ID),
                         ('run_id', RUN_ID), ('worker_incarnation', TOKEN), ('target_turn_id', TURN_ID)):
        _need(_matches(pattern, binding[key]), f'/{key}', 'pattern')
    _need(binding['how'] in HOWS, '/how', 'enum')
