# Model: Claude Opus 5.5 (claude-opus-5-5), lane SESSIONS.
"""Reads and writes of the lane's tables, through the event store's open transaction.

Every function takes ``tx``: the transaction the caller already owns (the dispatcher's ``ctx.tx`` in a
command, or one the service opened). Nothing here opens a transaction, calls a registry or daemon method,
or reaches an engine. Values are kept as JSON text and read back as the reader's own copies.

Every session, turn, queue item and delivery binding is checked (``records``) before it is written, so one the
schema refuses is never committed: the write raises, and the caller's transaction is rolled back. A delivery
row's later changes (its state, written flag and evidence) and the work intents are the lane's own private
rows, held to their columns' CHECK constraints.

A session is written whole or not at all: ``save(tx, before, after)`` writes the session's row only if its
revision is still the one ``before`` was read at (in a store whose transactions can overlap, a writer that
read an older revision changes nothing and raises ``Stale``), and then every turn that differs from
``before``.
"""
from __future__ import annotations

import json

from recursion_core.sessions import records


class Stale(RuntimeError):
    """The row changed since it was read in this transaction's view. Nothing was written."""


def _wire(value) -> str:
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(',', ':'))


def _read(text: str):
    return json.loads(text)


# ------------------------------------------------------------------------------------------- sessions
def insert_session(tx, record: dict) -> None:
    """A new session and its turns (a session starts with no turn, or with the first one)."""
    records.check_session_kept(record)
    head = {key: value for key, value in record.items() if key != 'turns'}
    tx.execute('INSERT INTO sessions_session (session_id, tenant, state, parent_session_id, unit_run_id, revision, '
               'record) VALUES (?, ?, ?, ?, ?, ?, ?)',
               (record['session_id'], record['tenant'], record['state'], record['parent_session_id'],
                _unit(record), record['revision'], _wire(head)))
    for turn in record['turns']:
        _insert_turn(tx, record['session_id'], turn)


def _unit(record: dict):
    return record['worker']['run_id'] if record['worker'] is not None else None


def _insert_turn(tx, session_id: str, turn: dict) -> None:
    tx.execute('INSERT INTO sessions_turn (turn_id, session_id, turn_index, record) VALUES (?, ?, ?, ?)',
               (turn['turn_id'], session_id, turn['index'], _wire(turn)))


def load(tx, session_id) -> dict | None:
    """The session.v1 record, with its turns oldest first, or None."""
    if not isinstance(session_id, str):
        return None
    rows = tx.execute('SELECT record FROM sessions_session WHERE session_id = ?', (session_id,))
    if not rows:
        return None
    record = _read(rows[0][0])
    turns = tx.execute('SELECT record FROM sessions_turn WHERE session_id = ? ORDER BY turn_index', (session_id,))
    record['turns'] = [_read(text) for (text,) in turns]
    return record


def tenant_of(tx, session_id) -> str | None:
    if not isinstance(session_id, str):
        return None
    rows = tx.execute('SELECT tenant FROM sessions_session WHERE session_id = ?', (session_id,))
    return rows[0][0] if rows else None


def save(tx, before: dict, after: dict) -> None:
    """Write ``after`` over ``before``: the session row, and each turn that is new or changed. ``after`` must keep
    the session's id, tenant and creation, and its turns may only be added to, never removed or reordered."""
    records.check_session_kept(after)
    for key in ('session_id', 'tenant', 'created_at', 'principal', 'parent_session_id', 'spawn_kind'):
        if after[key] != before[key]:
            raise records.RecordError(f'/{key}', 'immutable')
    if after['revision'] != before['revision'] + 1:
        raise records.RecordError('/revision', 'rises_by_one')
    old_turns = {turn['turn_id']: turn for turn in before['turns']}
    if [turn['turn_id'] for turn in after['turns'][:len(before['turns'])]] != [t['turn_id'] for t in before['turns']]:
        raise records.RecordError('/turns', 'append_only')
    head = {key: value for key, value in after.items() if key != 'turns'}
    changed = tx.execute('UPDATE sessions_session SET state = ?, unit_run_id = ?, revision = ?, record = ? '
                         'WHERE session_id = ? AND revision = ? RETURNING session_id',
                         (after['state'], _unit(after), after['revision'], _wire(head), after['session_id'],
                          before['revision']))
    if not changed:
        raise Stale(after['session_id'])
    for turn in after['turns']:
        old = old_turns.get(turn['turn_id'])
        if old is None:
            _insert_turn(tx, after['session_id'], turn)
        elif old != turn:
            tx.execute('UPDATE sessions_turn SET record = ? WHERE turn_id = ?', (_wire(turn), turn['turn_id']))


def page(tx, *, tenant=None, state=None, parent_session_id=None, after=None, limit: int = 100) -> tuple:
    """Sessions in id order, strictly after ``after``, at most ``limit``: ``(records, next_after or None)``."""
    clauses, params = [], []
    for column, value in (('tenant', tenant), ('state', state), ('parent_session_id', parent_session_id)):
        if value is not None:
            clauses.append(f'{column} = ?')
            params.append(value)
    if after is not None:
        clauses.append('session_id > ?')
        params.append(after)
    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    rows = tx.execute(f'SELECT session_id FROM sessions_session{where} ORDER BY session_id LIMIT ?',
                      (*params, limit + 1))
    ids = [row[0] for row in rows]
    more = len(ids) > limit
    ids = ids[:limit]
    return [load(tx, session_id) for session_id in ids], (ids[-1] if more and ids else None)


def with_unit(tx) -> list:
    """The ids of every session that has, or may have, a unit: one with a worker, whatever its state."""
    rows = tx.execute('SELECT session_id FROM sessions_session WHERE unit_run_id IS NOT NULL ORDER BY session_id')
    return [row[0] for row in rows]


def with_queued_items(tx) -> list:
    """The ids of every session that has a queued message, in id order."""
    rows = tx.execute("SELECT DISTINCT session_id FROM sessions_item WHERE state = 'queued' ORDER BY session_id")
    return [row[0] for row in rows]


# --------------------------------------------------------------------------------------------- items
def insert_item(tx, item: dict) -> None:
    records.check_item(item)
    tx.execute('INSERT INTO sessions_item (item_id, session_id, item_order, state, record) VALUES (?, ?, ?, ?, ?)',
               (item['item_id'], item['session_id'], item['order'], item['state'], _wire(item)))


def load_item(tx, item_id) -> dict | None:
    if not isinstance(item_id, str):
        return None
    rows = tx.execute('SELECT record FROM sessions_item WHERE item_id = ?', (item_id,))
    return _read(rows[0][0]) if rows else None


def save_item(tx, before: dict, after: dict) -> None:
    records.check_item(after)
    for key in ('item_id', 'session_id', 'order', 'created_at'):
        if after[key] != before[key]:
            raise records.RecordError(f'/{key}', 'immutable')
    if before['state'] in records.ITEM_FINAL and after != before:
        raise records.RecordError('/state', 'final')
    changed = tx.execute('UPDATE sessions_item SET state = ?, record = ? WHERE item_id = ? AND record = ? '
                         'RETURNING item_id', (after['state'], _wire(after), after['item_id'], _wire(before)))
    if not changed:
        raise Stale(after['item_id'])


def next_order(tx, session_id: str) -> int:
    (highest,), = tx.execute('SELECT COALESCE(MAX(item_order), 0) FROM sessions_item WHERE session_id = ?',
                             (session_id,))
    return highest + 1


def items(tx, session_id: str, states=None) -> list:
    """The session's items in their order, or only those in ``states``."""
    rows = tx.execute('SELECT record FROM sessions_item WHERE session_id = ? ORDER BY item_order', (session_id,))
    found = [_read(text) for (text,) in rows]
    return found if states is None else [item for item in found if item['state'] in states]


# ------------------------------------------------------------------------------------------ deliveries
def insert_delivery(tx, binding: dict, item_id) -> None:
    """The private binding, committed in the same write as the item's 'delivering' (or the turn's dispatch).
    ``item_id`` is the item's id, or None for a turn's own input; a binding that names an item names this one.
    The binding is checked whole first (a turn's own input is checked with the placeholder item id that
    ``delivery.begin_input`` uses)."""
    if 'item_id' in binding and binding['item_id'] != item_id:
        raise records.RecordError('/item_id', 'this_item')
    records.check_binding({**binding, 'item_id': 'qi-none' if item_id is None else item_id})
    tx.execute('INSERT INTO sessions_delivery (delivery_id, session_id, item_id, run_id, worker_incarnation, '
               'target_turn_id, how, state, written, evidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)',
               (binding['delivery_id'], binding['session_id'], item_id, binding['run_id'],
                binding['worker_incarnation'], binding['target_turn_id'], binding['how'], 'delivering', '[]'))


_DELIVERY_COLUMNS = ('delivery_id', 'session_id', 'item_id', 'run_id', 'worker_incarnation', 'target_turn_id',
                     'how', 'state', 'written', 'evidence')


def _delivery(row) -> dict:
    found = dict(zip(_DELIVERY_COLUMNS, row, strict=True))
    found['written'] = bool(found['written'])
    found['evidence'] = _read(found['evidence'])
    return found


def load_delivery(tx, delivery_id) -> dict | None:
    if not isinstance(delivery_id, str):
        return None
    rows = tx.execute(f'SELECT {", ".join(_DELIVERY_COLUMNS)} FROM sessions_delivery WHERE delivery_id = ?',
                      (delivery_id,))
    return _delivery(rows[0]) if rows else None


def deliveries(tx, *, state=None, session_id=None, target_turn_id=None) -> list:
    clauses, params = [], []
    for column, value in (('state', state), ('session_id', session_id), ('target_turn_id', target_turn_id)):
        if value is not None:
            clauses.append(f'{column} = ?')
            params.append(value)
    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    rows = tx.execute(f'SELECT {", ".join(_DELIVERY_COLUMNS)} FROM sessions_delivery{where} ORDER BY delivery_id',
                      tuple(params))
    return [_delivery(row) for row in rows]


def update_delivery(tx, delivery_id: str, *, state=None, written=None, evidence=None) -> None:
    sets, params = [], []
    if state is not None:
        sets.append('state = ?')
        params.append(state)
    if written is not None:
        sets.append('written = ?')
        params.append(1 if written else 0)
    if evidence is not None:
        sets.append('evidence = ?')
        params.append(_wire(evidence))
    if not sets:
        return
    changed = tx.execute(f'UPDATE sessions_delivery SET {", ".join(sets)} WHERE delivery_id = ? RETURNING delivery_id',
                         (*params, delivery_id))
    if not changed:
        raise Stale(delivery_id)


# ------------------------------------------------------------------------------------------ work intents
def insert_work(tx, operation_id: str, session_id: str, kind: str, payload: dict) -> None:
    tx.execute("INSERT INTO sessions_work (operation_id, session_id, kind, state, payload, outcome) "
               "VALUES (?, ?, ?, 'pending', ?, NULL)", (operation_id, session_id, kind, _wire(payload)))


_WORK_COLUMNS = ('operation_id', 'session_id', 'kind', 'state', 'payload', 'outcome')


def _work(row) -> dict:
    found = dict(zip(_WORK_COLUMNS, row, strict=True))
    found['payload'] = _read(found['payload'])
    found['outcome'] = None if found['outcome'] is None else _read(found['outcome'])
    return found


def load_work(tx, operation_id) -> dict | None:
    rows = tx.execute(f'SELECT {", ".join(_WORK_COLUMNS)} FROM sessions_work WHERE operation_id = ?',
                      (operation_id,))
    return _work(rows[0]) if rows else None


def work(tx, *, state=None, session_id=None) -> list:
    clauses, params = [], []
    for column, value in (('state', state), ('session_id', session_id)):
        if value is not None:
            clauses.append(f'{column} = ?')
            params.append(value)
    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    rows = tx.execute(f'SELECT {", ".join(_WORK_COLUMNS)} FROM sessions_work{where} ORDER BY rowid', tuple(params))
    return [_work(row) for row in rows]


def move_work(tx, operation_id: str, from_state: str, to_state: str, outcome=None) -> bool:
    """Move one work intent from one state to the next, only if it is still in ``from_state``. True when it moved:
    exactly one caller wins a claim."""
    changed = tx.execute('UPDATE sessions_work SET state = ?, outcome = ? WHERE operation_id = ? AND state = ? '
                         'RETURNING operation_id',
                         (to_state, None if outcome is None else _wire(outcome), operation_id, from_state))
    return bool(changed)
