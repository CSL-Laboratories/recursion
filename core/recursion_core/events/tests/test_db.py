"""The event store's transaction helper (events/db.py): its protocol, its fake, its lock order, receipts.

Every case in ``Cases`` runs twice: against ``EventStore`` over a real file and against ``FakeStore``. The
fake is the helper's own code with durability off, so a refusal tested here is a refusal of both. Crash and
full-disk behaviour is in test_durability.py.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
import tempfile
import threading
import unittest

from recursion_core.events import db as D

BOUND_S = 30.0          # only ends a wait that would otherwise never end; it never decides a pass
NOT_YET_S = 0.2         # used only to check that something has NOT happened yet
WORK = 'CREATE TABLE work (command_id TEXT NOT NULL, operation_id TEXT NOT NULL, state TEXT NOT NULL)'
QUEUE = 'CREATE TABLE queue_items (item_id TEXT PRIMARY KEY, state TEXT NOT NULL)'
CMD, CMD2 = 'command-0001', 'command-0002'


def digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def event(kind='result', log='run:run-1', run='run-1', turn=None, source='core', trust='trusted', payload=None, **extra):
    out = {'session_id': log, 'run_id': run, 'turn_id': turn, 'type': kind, 'source': source, 'trust': trust,
           'payload': {} if payload is None else payload}
    out.update(extra)
    return out


def dispatch(store, tenant, method, command_id, params, handler):
    """What SPINE's S1 dispatcher does with the helper, reduced to the receipt rule: look the receipt up IN the
    transaction, before anything else; the same digest returns the first answer, another digest is conflict;
    otherwise call the handler with the transaction and keep its answer in the same transaction."""
    payload = digest(json.dumps(params, sort_keys=True))
    with store.transaction() as tx:
        kept = tx.receipt_get(tenant, method, command_id)
        if kept is not None:
            if kept.payload_digest == payload:
                return kept.answer
            return {'code': 'conflict', 'detail': 'command_id_reused'}
        answer = handler(tx, params)
        tx.receipt_put(tenant, method, command_id, payload, answer)
        return answer


def committed_events(store) -> list:
    """What a reader on a connection of its own sees as committed in the log."""
    with contextlib.closing(sqlite3.connect(store.path)) as conn:
        return [json.loads(text) for (text,) in conn.execute('SELECT event FROM event_log ORDER BY position')]


def committed(store, sql, params=()) -> list:
    with contextlib.closing(sqlite3.connect(store.path)) as conn:
        return conn.execute(sql, params).fetchall()


class Announcing:
    """Stands in for the helper's lock and says when a thread has asked for it, and how many have."""

    def __init__(self, lock) -> None:
        self._lock, self.asked = lock, threading.Event()
        self._count, self._changed = 0, threading.Condition()

    def acquire(self, *args, **kwargs):
        with self._changed:
            self._count += 1
            self._changed.notify_all()
        self.asked.set()
        return self._lock.acquire(*args, **kwargs)

    def wait_for_askers(self, count: int) -> bool:
        with self._changed:
            return self._changed.wait_for(lambda: self._count >= count, BOUND_S)

    def release(self):
        self._lock.release()

    def locked(self):
        return self._lock.locked()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


class FailingConnection:
    """Stands in for the writer connection: the named statement raises the given error, the rest pass."""

    def __init__(self, conn, statement, error) -> None:
        self._conn, self._statement, self._error = conn, statement, error

    def execute(self, sql, *args):
        if sql.startswith(self._statement):
            raise self._error
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def io_error() -> sqlite3.OperationalError:
    error = sqlite3.OperationalError('disk I/O error')
    error.sqlite_errorcode, error.sqlite_errorname = 10, 'SQLITE_IOERR'
    return error


class Cases:
    """The helper's protocol. ``make()`` returns an open store with lane ``sessions`` declaring ``work``."""

    def make(self, declare=True):
        raise NotImplementedError

    def refused(self, use, kind=D.StoreError) -> str:
        with self.assertRaises(kind) as caught:
            use()
        return str(caught.exception)

    # -- the published names

    def test_the_published_names_and_no_other_way_in(self) -> None:
        store = self.make()
        with store.transaction() as tx:
            self.assertEqual({name for name in dir(tx) if not name.startswith('_')},
                             {'receipt_get', 'receipt_put', 'append', 'execute'})
        for name in ('transaction', 'register_tables', 'subscribe', 'add_lock_probe', 'close'):
            self.assertTrue(callable(getattr(store, name)), name)

    # -- one transaction: all of it or none of it

    def test_a_commit_keeps_everything_and_an_abort_midway_leaves_nothing(self) -> None:
        store = self.make()
        store.register_tables('queue', 1, QUEUE)
        with store.transaction() as tx:
            tx.execute('INSERT INTO work VALUES (?, ?, ?)', (CMD, 'op-1', 'pending'))
            tx.execute('INSERT INTO queue_items VALUES (?, ?)', ('item-1', 'queued'))
            tx.receipt_put('t1', 'session.turn', CMD, digest('a'), {'code': 'ok'})
            self.assertEqual(tx.append(event()), 1)
        before = (committed(store, 'SELECT * FROM work'), committed(store, 'SELECT * FROM queue_items'),
                  committed(store, 'SELECT * FROM receipts'), committed_events(store))
        self.assertEqual([len(part) for part in before], [1, 1, 1, 1])
        with self.assertRaises(RuntimeError):
            with store.transaction() as tx:
                tx.execute('INSERT INTO work VALUES (?, ?, ?)', (CMD2, 'op-2', 'pending'))
                tx.execute("UPDATE queue_items SET state = 'delivering' WHERE item_id = 'item-1'")
                tx.receipt_put('t1', 'session.turn', CMD2, digest('b'), {'code': 'ok'})
                tx.append(event())
                raise RuntimeError('the process gives up halfway')
        after = (committed(store, 'SELECT * FROM work'), committed(store, 'SELECT * FROM queue_items'),
                 committed(store, 'SELECT * FROM receipts'), committed_events(store))
        self.assertEqual(after, before, 'no row of any lane table, no receipt and no event of the aborted one')
        with store.transaction() as tx:
            self.assertEqual(tx.append(event()), 2, 'and the aborted append took no seq')

    def test_a_second_transaction_in_the_same_thread_raises_and_does_not_wait(self) -> None:
        store = self.make()
        with store.transaction():
            self.assertEqual(self.refused(lambda: store.transaction().__enter__()), 'nested_transaction')
            self.assertEqual(self.refused(lambda: store.register_tables('queue', 1, QUEUE)), 'nested_transaction')
            other = FakeOnly.other_store(self)
            self.assertEqual(self.refused(lambda: other.transaction().__enter__()), 'nested_transaction',
                             'one thread holds one transaction of any event store')
        with store.transaction():           # and the thread can open one again afterwards
            pass

    def test_a_transaction_is_used_by_its_owner_and_only_until_it_exits(self) -> None:
        store = self.make()
        with store.transaction() as kept:
            seen = []
            thread = threading.Thread(target=lambda: seen.append(self.refused(lambda: kept.receipt_get('t', 'm', CMD))))
            thread.start()
            thread.join(BOUND_S)
            self.assertEqual(seen, ['transaction_not_active_for_caller'])
        for use in (lambda: kept.receipt_get('t', 'm', CMD), lambda: kept.receipt_put('t', 'm', CMD, digest('x'), {}),
                    lambda: kept.append(event()), lambda: kept.execute('SELECT * FROM work')):
            self.assertEqual(self.refused(use), 'transaction_not_active_for_caller')

    def test_one_owner_at_a_time_across_threads(self) -> None:
        store = self.make()
        with store.transaction() as tx:
            tx.execute('INSERT INTO work VALUES (?, ?, ?)', ('counter', '0', 'count'))
        threads, rounds = 4, 25

        def bump():
            for _ in range(rounds):
                with store.transaction() as tx:
                    (value,), = tx.execute("SELECT operation_id FROM work WHERE command_id = 'counter'")
                    tx.execute("UPDATE work SET operation_id = ? WHERE command_id = 'counter'", (str(int(value) + 1),))
        workers = [threading.Thread(target=bump) for _ in range(threads)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(BOUND_S)
        self.assertEqual(committed(store, "SELECT operation_id FROM work WHERE command_id = 'counter'"),
                         [(str(threads * rounds),)], 'every read, modify and write had the store to itself')

    def test_a_second_owner_waits_for_the_first_to_end(self) -> None:
        store = self.make()
        inside, release, entered = threading.Event(), threading.Event(), threading.Event()

        def first():
            with store.transaction():
                inside.set()
                release.wait(BOUND_S)
        holder = threading.Thread(target=first)
        holder.start()
        self.assertTrue(inside.wait(BOUND_S))
        store._lock = announcing = Announcing(store._lock)

        def second():
            with store.transaction():
                entered.set()
        waiter = threading.Thread(target=second)
        waiter.start()
        self.assertTrue(announcing.asked.wait(BOUND_S), 'the second has asked for the helper')
        self.assertFalse(entered.wait(NOT_YET_S), 'and has not entered while the first is open')
        release.set()
        holder.join(BOUND_S)
        self.assertTrue(entered.wait(BOUND_S))
        waiter.join(BOUND_S)

    # -- the log

    def test_seq_is_assigned_here_gapless_per_log(self) -> None:
        store = self.make()
        with store.transaction() as tx:
            seqs = [tx.append(event(log=log, run=log[4:])) for log in ('run:a', 'run:b', 'run:a', 'run:a', 'run:b')]
        self.assertEqual(seqs, [1, 1, 2, 3, 2])
        with store.transaction() as tx:
            self.assertEqual(tx.append(event(log='ses-one', run=None)), 1)
            self.assertEqual(tx.append(event(log='run:a', run='a')), 4)
        stored = committed_events(store)
        self.assertEqual([(e['session_id'], e['seq']) for e in stored],
                         [('run:a', 1), ('run:b', 1), ('run:a', 2), ('run:a', 3), ('run:b', 2), ('ses-one', 1), ('run:a', 4)])
        for e in stored:
            self.assertRegex(e['time'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$')
        for key in ('seq', 'time'):
            with store.transaction() as tx:
                self.assertEqual(self.refused(lambda: tx.append(event(**{key: 1})), ValueError), f'assigned_by_the_store:{key}')

    def test_time_is_the_stores_clock_in_milliseconds(self) -> None:
        store = self.make()
        store._clock = lambda: 1794614400.1239      # 2026-11-14T00:00:00.123Z
        with store.transaction() as tx:
            tx.append(event())
        self.assertEqual(committed_events(store)[0]['time'], '2026-11-14T00:00:00.123Z')

    def test_the_envelope_rules_of_event_v1(self) -> None:
        store = self.make()
        refused = {
            'type_unknown': event(kind='made.up'),
            'engine_or_plugin_content_is_untrusted': event(kind='tool.call', source='engine', trust='trusted'),
            'source_not_allowed_for:state.changed': event(kind='state.changed', source='client', trust='trusted'),
            'trust_not_allowed_for:approval.requested': event(kind='approval.requested', trust='untrusted'),
            'source_not_allowed_for:approval.requested': event(kind='approval.requested', source='engine', trust='untrusted'),
            'run_id_required_for:engine.output': event(kind='engine.output', source='engine', trust='untrusted', run=None),
            'turn_id_required_for:turn.started': event(kind='turn.started'),
            'session_id_not_a_log_id': event(log='session-1'),
            'run_id_invalid': event(run='has space'),
            'turn_id_invalid': event(turn='t-1'),
            'source_unknown': event(source='daemon'),
            'trust_unknown': event(trust='maybe'),
            'unknown_field:extra': event(extra=1),
            'missing_field:turn_id': {k: v for k, v in event().items() if k != 'turn_id'},
            'command_id_invalid': event(command_id='short'),
            'causation_id_invalid': event(causation_id='has space'),
        }
        for code, bad in refused.items():
            with self.subTest(code=code):
                with store.transaction() as tx:
                    self.assertEqual(self.refused(lambda: tx.append(bad), ValueError), code)
        with store.transaction() as tx:
            self.assertEqual(self.refused(lambda: tx.append(event(payload=['not', 'an', 'object'])), TypeError),
                             'a payload is an object')
            self.assertEqual(self.refused(lambda: tx.append('not an event'), TypeError), 'an event is an object')
        self.assertEqual(committed_events(store), [])
        accepted = [event(kind='engine.output', source='engine', trust='untrusted'),
                    event(kind='turn.started', turn='turn-1'),
                    event(kind='usage', source='engine', trust='untrusted', payload={'value': None, 'quality': 'unknown'}),
                    event(kind='state.changed', command_id=CMD, correlation_id='c-1', causation_id='3')]
        with store.transaction() as tx:
            self.assertEqual([tx.append(e) for e in accepted], [1, 2, 3, 4])

    def test_what_is_written_and_read_is_owned_and_only_json_values(self) -> None:
        store = self.make()
        answer, kept = {'code': 'ok', 'items': []}, event(payload={'ids': []})
        with store.transaction() as tx:
            tx.receipt_put('t1', 'session.turn', CMD, digest('a'), answer)
            tx.append(kept)
            answer['items'].append('changed after the write')
            kept['payload']['ids'].append('changed after the write')
        with store.transaction() as tx:
            got = tx.receipt_get('t1', 'session.turn', CMD)
            self.assertEqual(got, (digest('a'), {'code': 'ok', 'items': []}))
            got.answer['items'].append('changed after the read')
            self.assertEqual(tx.receipt_get('t1', 'session.turn', CMD).answer, {'code': 'ok', 'items': []})
        self.assertEqual(committed_events(store)[0]['payload'], {'ids': []})
        bad_values = {'an object': object(), 'not a number': float('nan'), 'infinity': float('inf'), 'a set': {1, 2},
                      'a tuple': (1, 2), 'a key that is not text': {1: 'one'}, 'a lone surrogate': '\udc80',
                      'too deep': json.loads('[' * 70 + ']' * 70)}
        for what, value in bad_values.items():
            for write in (lambda tx: tx.receipt_put('t1', 'session.turn', CMD2, digest('b'), {'v': value}),
                          lambda tx: tx.append(event(payload={'v': value}))):
                with self.subTest(value=what):
                    with self.assertRaises((TypeError, ValueError)):
                        with store.transaction() as tx:
                            write(tx)
        self.assertEqual(len(committed(store, 'SELECT * FROM receipts')), 1)
        self.assertEqual(len(committed_events(store)), 1)

    # -- receipts

    def test_a_receipt_is_kept_read_back_and_never_replaced(self) -> None:
        store = self.make()
        with store.transaction() as tx:
            self.assertIsNone(tx.receipt_get('t1', 'session.turn', CMD))
            tx.receipt_put('t1', 'session.turn', CMD, digest('a'), {'code': 'ok', 'n': 1})
            self.assertEqual(tx.receipt_get('t1', 'session.turn', CMD), D.Receipt(digest('a'), {'code': 'ok', 'n': 1}))
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            with store.transaction() as tx:
                tx.receipt_put('t1', 'session.turn', CMD, digest('b'), {'code': 'conflict'})
        self.assertIsInstance(caught.exception, D.ReceiptExists)
        with store.transaction() as tx:
            self.assertEqual(tx.receipt_get('t1', 'session.turn', CMD), (digest('a'), {'code': 'ok', 'n': 1}))
            # The key is all three parts: another tenant or another method is another receipt.
            tx.receipt_put('t2', 'session.turn', CMD, digest('b'), {'code': 'ok', 'n': 2})
            tx.receipt_put('t1', 'session.steer', CMD, digest('c'), {'code': 'ok', 'n': 3})
        self.assertEqual(len(committed(store, 'SELECT * FROM receipts')), 3)

    def test_a_receipt_is_keyed_and_digested_as_the_protocol_says(self) -> None:
        store = self.make()
        bad = [('', 'session.turn', CMD, digest('a')), ('t1', '', CMD, digest('a')), ('t1', 'has space', CMD, digest('a')),
               ('t1', 'session.turn', 'short', digest('a')), ('t1', 'session.turn', 7, digest('a')),
               ('t1', 'session.turn', CMD, 'digest-1'), ('t1', 'session.turn', CMD, digest('a').upper()),
               ('t\x00', 'session.turn', CMD, digest('a'))]
        for tenant, method, command_id, payload in bad:
            with self.subTest(key=(tenant, method, command_id, payload)):
                with store.transaction() as tx:
                    with self.assertRaises(TypeError):
                        tx.receipt_put(tenant, method, command_id, payload, {'code': 'ok'})
        self.assertEqual(committed(store, 'SELECT * FROM receipts'), [])

    def test_a_duplicate_command_id_returns_the_identical_answer_and_another_payload_is_conflict(self) -> None:
        store = self.make()
        calls = []

        def handler(tx, params):
            calls.append(params)
            tx.execute('INSERT INTO work VALUES (?, ?, ?)', (CMD, 'op-%d' % len(calls), 'pending'))
            return {'code': 'ok', 'operation_id': 'op-%d' % len(calls), 'nested': {'list': [1, 2]}}
        first = dispatch(store, 't1', 'session.turn', CMD, {'text': 'hello'}, handler)
        again = dispatch(store, 't1', 'session.turn', CMD, {'text': 'hello'}, handler)
        other = dispatch(store, 't1', 'session.turn', CMD, {'text': 'something else'}, handler)
        self.assertEqual(first, {'code': 'ok', 'operation_id': 'op-1', 'nested': {'list': [1, 2]}})
        self.assertEqual(again, first, 'the identical answer')
        self.assertEqual(other, {'code': 'conflict', 'detail': 'command_id_reused'})
        self.assertEqual((len(calls), committed(store, 'SELECT operation_id FROM work')), (1, [('op-1',)]),
                         'the handler ran once and its work was written once')

    def test_two_concurrent_calls_with_one_command_id_find_one_receipt(self) -> None:
        store = self.make()
        inside, release, calls, results = threading.Event(), threading.Event(), [], {}

        def handler(tx, params):
            calls.append(threading.current_thread().name)
            tx.execute('INSERT INTO work VALUES (?, ?, ?)', (CMD, 'op-1', 'pending'))
            inside.set()
            release.wait(BOUND_S)
            return {'code': 'ok', 'operation_id': 'op-1'}

        def call(name):
            results[name] = dispatch(store, 't1', 'session.turn', CMD, {'text': 'hello'}, handler)
        first = threading.Thread(target=call, args=('first',), name='first')
        first.start()
        self.assertTrue(inside.wait(BOUND_S), 'the first is inside its handler, its transaction open')
        store._lock = announcing = Announcing(store._lock)
        second = threading.Thread(target=call, args=('second',), name='second')
        second.start()
        self.assertTrue(announcing.asked.wait(BOUND_S), 'the second waits for the helper')
        second.join(NOT_YET_S)
        self.assertTrue(second.is_alive() and 'second' not in results, 'and has not answered')
        release.set()
        first.join(BOUND_S)
        second.join(BOUND_S)
        self.assertEqual(calls, ['first'], 'the second found the first one\'s receipt and called nothing')
        self.assertEqual(results, {'first': {'code': 'ok', 'operation_id': 'op-1'}, 'second': {'code': 'ok', 'operation_id': 'op-1'}})
        self.assertEqual(committed(store, 'SELECT * FROM work'), [(CMD, 'op-1', 'pending')])

    # -- execute: only declared tables, only rows

    def test_execute_reaches_only_rows_of_declared_tables(self) -> None:
        store = self.make()
        store.register_tables('queue', 1, 'CREATE TABLE counted (id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT)')
        with store.transaction() as tx:
            tx.receipt_put('t1', 'session.turn', CMD, digest('a'), {'code': 'ok'})
            tx.append(event())
            tx.execute('INSERT INTO work VALUES (?, ?, ?)', (CMD, 'op-1', 'pending'))
        before = (committed(store, 'SELECT * FROM receipts'), committed_events(store), committed(store, 'SELECT * FROM work'))
        refused = ['INSERT INTO undeclared VALUES (1)', 'SELECT * FROM receipts', 'DELETE FROM receipts',
                   'SELECT * FROM event_log', 'UPDATE event_logs SET last_seq = 0', 'SELECT * FROM lane_tables',
                   'SELECT * FROM store_meta', 'SELECT name FROM sqlite_master', 'UPDATE sqlite_sequence SET seq = 0',
                   'CREATE TABLE made_in_a_handler (a TEXT)', 'CREATE TEMP TABLE scratch (a TEXT)', 'DROP TABLE work',
                   'ALTER TABLE work ADD COLUMN extra TEXT', 'PRAGMA journal_mode=DELETE', 'PRAGMA synchronous=OFF',
                   "ATTACH DATABASE ':memory:' AS other", 'COMMIT', 'ROLLBACK', 'SAVEPOINT s', 'BEGIN',
                   'CREATE INDEX on_work ON work (state)', 'SELECT * FROM work; DELETE FROM work']
        for statement in refused:
            with self.subTest(statement=statement):
                with self.assertRaises((sqlite3.Error, D.StoreError)):
                    with store.transaction() as tx:
                        tx.execute(statement)
        self.assertEqual((committed(store, 'SELECT * FROM receipts'), committed_events(store),
                          committed(store, 'SELECT * FROM work')), before)
        with store.transaction() as tx:
            with self.assertRaises(sqlite3.DatabaseError):
                tx.execute('DELETE FROM receipts')
            # A refused statement fails alone: the transaction goes on and commits.
            self.assertEqual(tx.execute("UPDATE work SET state = 'done' WHERE command_id = ? RETURNING operation_id", (CMD,)),
                             [('op-1',)])
            self.assertEqual(tx.execute("INSERT INTO counted (note) VALUES ('x') RETURNING id"), [(1,)])
            self.assertEqual(tx.execute('SELECT command_id, state FROM work'), [(CMD, 'done')])
        self.assertEqual(committed(store, 'SELECT state FROM work'), [('done',)])

    def test_the_reads_a_lane_writes_are_allowed_on_its_tables(self) -> None:
        store = self.make()
        store.register_tables('queue', 1, 'CREATE TABLE items (k INTEGER PRIMARY KEY, owner TEXT NOT NULL); '
                                          'CREATE INDEX items_by_owner ON items (owner)')
        with store.transaction() as tx:
            for k, owner in ((2, CMD), (1, CMD2), (3, CMD)):
                tx.execute('INSERT INTO items VALUES (?, ?)', (k, owner))
            tx.execute('INSERT INTO work VALUES (?, ?, ?)', (CMD, 'op-1', 'pending'))
        with store.transaction() as tx:
            self.assertEqual(tx.execute('SELECT k FROM items ORDER BY k'), [(1,), (2,), (3,)])
            self.assertEqual(tx.execute('SELECT k FROM items ORDER BY k DESC LIMIT 1'), [(3,)])
            self.assertEqual(tx.execute('SELECT count(*), max(k) FROM items WHERE owner = ?', (CMD,)), [(2, 3)])
            self.assertEqual(tx.execute('SELECT i.k, w.state FROM items i JOIN work w ON w.command_id = i.owner ORDER BY i.k'),
                             [(2, 'pending'), (3, 'pending')])
            self.assertEqual(tx.execute('SELECT k FROM items WHERE owner IN (SELECT command_id FROM work) ORDER BY k'),
                             [(2,), (3,)])
            self.assertEqual(tx.execute('WITH mine AS (SELECT k FROM items WHERE owner = ?) SELECT sum(k) FROM mine', (CMD,)),
                             [(5,)])

    # -- tables of the lanes

    def test_a_lane_declares_once_per_process_with_a_valid_name_version_and_text(self) -> None:
        store = self.make()
        self.assertEqual(self.refused(lambda: store.register_tables('sessions', 1, WORK)), 'declared_once')
        self.assertEqual(self.refused(lambda: store.register_tables('sessions', 2, 'CREATE TABLE more (a TEXT)')), 'declared_once')
        for lane, version, ddl in (('Queue', 1, QUEUE), ('', 1, QUEUE), ('queue', 0, QUEUE), ('queue', True, QUEUE),
                                   ('queue', '1', QUEUE), ('queue', 1, ''), ('queue', 1, None)):
            with self.subTest(lane=lane, version=version, ddl=ddl):
                with self.assertRaises(TypeError):
                    store.register_tables(lane, version, ddl)
        store.register_tables('queue', 1, QUEUE)

    def test_a_declaration_can_only_declare_and_leaves_nothing_behind(self) -> None:
        store = self.make()
        with store.transaction() as tx:
            tx.receipt_put('t1', 'session.turn', CMD, digest('a'), {'code': 'ok'})
            tx.execute('INSERT INTO work VALUES (?, ?, ?)', (CMD, 'op-1', 'pending'))
            tx.append(event())
        before = (committed(store, 'SELECT * FROM receipts'), committed(store, 'SELECT * FROM work'), committed_events(store))
        refused = {
            "a write to the helper's table": "UPDATE receipts SET payload_digest = 'x'; CREATE TABLE own (id TEXT)",
            'a statement that is not SQL': 'CREATE TABLE leaked (id TEXT); THIS IS NOT SQL',
            'a statement that fails after a table was made': 'CREATE TABLE leaked (id TEXT); CREATE TABLE leaked (id TEXT)',
            'a view': 'CREATE TABLE seen (id TEXT); CREATE VIEW peek AS SELECT * FROM receipts',
            'a trigger': 'CREATE TABLE t1 (id TEXT); CREATE TRIGGER sneak AFTER INSERT ON t1 BEGIN DELETE FROM receipts; END',
            "a change to another lane's table": 'CREATE TABLE mine (id TEXT); ALTER TABLE work ADD COLUMN extra TEXT',
            "another lane's table dropped": 'CREATE TABLE mine (id TEXT); DROP TABLE work',
            "an index on another lane's table": 'CREATE TABLE mine (id TEXT); CREATE INDEX on_work ON work (state)',
            "an index on the helper's table": 'CREATE TABLE mine (id TEXT); CREATE INDEX on_log ON event_log (seq)',
            'rows written by a declaration': "CREATE TABLE mine (id TEXT); INSERT INTO mine VALUES ('x')",
            "the helper's table read": 'CREATE TABLE mine AS SELECT * FROM receipts',
            "the helper's table declared again, quietly": 'CREATE TABLE mine (id TEXT); CREATE TABLE IF NOT EXISTS receipts (id TEXT)',
            "the helper's meta table": 'CREATE TABLE store_meta (id TEXT)',
            'a temporary table': 'CREATE TEMP TABLE mine (id TEXT)',
            'a pragma': 'CREATE TABLE mine (id TEXT); PRAGMA foreign_keys=OFF',
            'an attached file': "ATTACH DATABASE ':memory:' AS other; CREATE TABLE other.mine (id TEXT)",
            'no table at all': 'SELECT 1',
        }
        made = {'own', 'leaked', 'seen', 'peek', 't1', 'sneak', 'mine', 'on_work', 'on_log'}
        for what, ddl in refused.items():
            with self.subTest(declaration=what):
                with self.assertRaises((sqlite3.Error, D.StoreError)):
                    store.register_tables('queue', 1, ddl)
                names = {name for (name,) in committed(store, 'SELECT name FROM sqlite_master')}
                self.assertEqual(names & made, set(), 'nothing of a refused declaration is left')
                self.assertEqual((committed(store, 'SELECT * FROM receipts'), committed(store, 'SELECT * FROM work'),
                                  committed_events(store)), before)
        store.register_tables('queue', 1, 'CREATE TABLE queue_items (item_id TEXT PRIMARY KEY, state TEXT); '
                                          'CREATE INDEX by_state ON queue_items (state);')
        with store.transaction() as tx:
            tx.execute('INSERT INTO queue_items VALUES (?, ?)', ('item-1', 'queued'))
            self.assertEqual(tx.execute('SELECT item_id FROM queue_items WHERE state = ?', ('queued',)), [('item-1',)])

    def test_a_foreign_key_a_declaration_states_is_enforced(self) -> None:
        store = self.make()
        store.register_tables('queue', 1, 'CREATE TABLE parents (id TEXT PRIMARY KEY); '
                                          'CREATE TABLE children (id TEXT PRIMARY KEY, parent TEXT NOT NULL REFERENCES parents (id))')
        with self.assertRaises(sqlite3.IntegrityError):
            with store.transaction() as tx:
                tx.execute('INSERT INTO children VALUES (?, ?)', ('child-1', 'no-such-parent'))
        with store.transaction() as tx:
            tx.execute('INSERT INTO parents VALUES (?)', ('parent-1',))
            tx.execute('INSERT INTO children VALUES (?, ?)', ('child-1', 'parent-1'))
        self.assertEqual(committed(store, 'SELECT * FROM children'), [('child-1', 'parent-1')])

    # -- after the commit

    def test_a_subscriber_runs_only_after_the_commit_and_outside_the_helpers_lock(self) -> None:
        store = self.make()
        seen = []

        def subscriber(events):
            free = store._lock.acquire(blocking=False)
            if free:
                store._lock.release()
            seen.append({'events': [(e['session_id'], e['seq'], e['type']) for e in events],
                         'visible to another reader': len(committed_events(store)),
                         'helper lock free': free, 'in a transaction': D.in_transaction()})
            if free:
                with store.transaction() as tx:         # a subscriber may write: no lock of the helper is held
                    tx.execute('INSERT INTO work VALUES (?, ?, ?)', ('from-subscriber', 'op', str(len(seen))))
        store.subscribe(subscriber)
        with self.assertRaises(RuntimeError):
            with store.transaction() as tx:
                tx.append(event())
                raise RuntimeError('rolled back')
        self.assertEqual(seen, [], 'a rollback publishes nothing')
        with store.transaction() as tx:
            tx.execute('INSERT INTO work VALUES (?, ?, ?)', (CMD, 'op-1', 'no event'))
        self.assertEqual(seen, [], 'a commit with no event publishes nothing')
        with store.transaction() as tx:
            tx.append(event())
            tx.append(event(kind='usage', log='ses-a', run=None))
            self.assertEqual(seen, [], 'nothing before the commit')
        self.assertEqual(seen, [{'events': [('run:run-1', 1, 'result'), ('ses-a', 1, 'usage')], 'visible to another reader': 2,
                                 'helper lock free': True, 'in a transaction': False}])
        self.assertEqual(committed(store, "SELECT state FROM work WHERE command_id = 'from-subscriber'"), [('1',)])

    def test_subscribers_see_commits_in_order_even_when_another_thread_commits_meanwhile(self) -> None:
        store = self.make()
        delivered, first_in_subscriber, go_on = [], threading.Event(), threading.Event()

        def subscriber(events):
            delivered.append([e['seq'] for e in events])
            if events[0]['seq'] == 1:
                first_in_subscriber.set()
                go_on.wait(BOUND_S)

        store.subscribe(subscriber)

        def commit():
            with store.transaction() as tx:
                tx.append(event())
        first = threading.Thread(target=commit)
        first.start()
        self.assertTrue(first_in_subscriber.wait(BOUND_S), 'commit 1 is being delivered')
        second = threading.Thread(target=commit)
        second.start()
        second.join(BOUND_S)
        self.assertFalse(second.is_alive(), 'commit 2 returned while commit 1 was still being delivered')
        self.assertEqual(delivered, [[1]], 'and commit 2 was not delivered before commit 1')
        go_on.set()
        first.join(BOUND_S)
        self.assertEqual(delivered, [[1], [2]])

    def test_a_subscriber_that_raises_affects_neither_the_commit_nor_the_others(self) -> None:
        store = self.make()
        seen = []
        store.subscribe(lambda events: (_ for _ in ()).throw(RuntimeError('subscriber bug')))
        unsubscribe = store.subscribe(lambda events: seen.append(len(events)))
        with self.assertLogs(D.LOG, 'ERROR'):
            with store.transaction() as tx:
                tx.append(event())
        self.assertEqual((seen, len(committed_events(store))), ([1], 1))
        unsubscribe()
        with self.assertLogs(D.LOG, 'ERROR'):
            with store.transaction() as tx:
                tx.append(event())
        self.assertEqual(seen, [1], 'an unsubscribed function is not called')

    # -- lock order

    def test_the_helpers_lock_is_never_taken_under_an_outer_lock(self) -> None:
        store = self.make()
        registry_lock, held = threading.RLock(), threading.local()

        @contextlib.contextmanager
        def registry():
            D.require_no_transaction('daemon.registry')     # what the registry's code does before its lock
            with registry_lock:
                held.depth = getattr(held, 'depth', 0) + 1
                try:
                    yield
                finally:
                    held.depth -= 1
        store.add_lock_probe('daemon.registry', lambda: getattr(held, 'depth', 0) > 0)
        store._lock = announcing = Announcing(store._lock)
        with registry():
            self.assertEqual(self.refused(lambda: store.transaction().__enter__(), D.LockOrderError), 'lock_order:daemon.registry')
            self.assertEqual(self.refused(lambda: store.register_tables('queue', 1, QUEUE), D.LockOrderError),
                             'lock_order:daemon.registry')
        self.assertFalse(announcing.asked.is_set(), 'the helper\'s lock was not even asked for')
        with store.transaction():
            self.assertEqual(self.refused(lambda: registry().__enter__(), D.LockOrderError), 'lock_order:daemon.registry')
            self.assertTrue(D.in_transaction())
        self.assertFalse(D.in_transaction())
        with registry():
            pass                                            # outside a transaction the registry is free

    def test_a_lock_probe_that_fails_refuses(self) -> None:
        store = self.make()
        store.add_lock_probe('run', lambda: 1 / 0)
        with self.assertRaises(D.LockOrderError) as caught:
            with store.transaction():
                pass
        self.assertEqual(str(caught.exception), 'lock_order:run')
        self.assertIsInstance(caught.exception.__cause__, ZeroDivisionError)

    # -- closing

    def test_a_close_is_refused_while_a_transaction_is_open_and_a_closed_store_refuses(self) -> None:
        store = self.make()
        with store.transaction() as tx:
            tx.execute('INSERT INTO work VALUES (?, ?, ?)', (CMD, 'op-1', 'written before the close'))
            self.assertEqual(self.refused(store.close), 'transactions_active')
            tx.append(event())
        self.assertEqual(committed(store, 'SELECT state FROM work'), [('written before the close',)])
        store.close()
        store.close()                                       # a second close does nothing
        self.assertEqual(self.refused(lambda: store.transaction().__enter__()), 'store_closed')
        self.assertEqual(self.refused(lambda: store.register_tables('queue', 1, QUEUE)), 'store_closed')
        self.assertEqual(self.refused(lambda: tx.append(event())), 'transaction_not_active_for_caller')

    def test_a_transaction_that_waited_while_the_store_closed_is_refused_when_its_turn_comes(self) -> None:
        store = self.make()
        lock = store._lock
        store._lock = announcing = Announcing(lock)
        lock.acquire()                                      # stands for a transaction in flight
        outcome = []

        def later():
            try:
                with store.transaction():
                    outcome.append('entered')
            except D.StoreError as exc:
                outcome.append(str(exc))
        thread = threading.Thread(target=later)
        thread.start()
        self.assertTrue(announcing.wait_for_askers(1), 'the transaction waits for its turn')
        closer = threading.Thread(target=store.close)       # nothing is counted open: the close goes ahead
        closer.start()
        self.assertTrue(announcing.wait_for_askers(2), 'the close has marked the store closed and waits too')
        self.assertTrue(store._closed)
        lock.release()
        thread.join(BOUND_S)
        closer.join(BOUND_S)
        self.assertEqual(outcome, ['store_closed'])

    # -- failures

    def test_a_full_database_fails_the_statement_and_leaves_the_store_usable(self) -> None:
        store = self.make()
        (pages,), = store._conn.execute('PRAGMA page_count').fetchall()
        store._conn.execute(f'PRAGMA max_page_count = {pages + 2}')
        with self.assertRaises(sqlite3.OperationalError) as caught:
            with store.transaction() as tx:
                tx.append(event())
                tx.execute('INSERT INTO work VALUES (?, ?, ?)', (CMD, 'op-1', 'x' * 200_000))
        self.assertEqual(caught.exception.sqlite_errorname, 'SQLITE_FULL')
        self.assertEqual((committed_events(store), committed(store, 'SELECT * FROM work')), ([], []))
        store._conn.execute('PRAGMA max_page_count = 1073741823')
        with store.transaction() as tx:
            self.assertEqual(tx.append(event()), 1, 'usable again once there is room, and the failed append took no seq')

    def test_a_transaction_sqlite_ended_on_its_own_refuses_every_later_write(self) -> None:
        store = self.make()
        for swallow in (False, True):
            with self.subTest(swallow=swallow):
                with self.assertRaises(D.StoreError) as caught:
                    with store.transaction() as tx:
                        tx.execute('INSERT INTO work VALUES (?, ?, ?)', (CMD, 'op-1', 'before'))
                        store._conn.execute('ROLLBACK')     # stands for SQLite rolling back on a full disk or I/O error
                        try:
                            tx.execute('INSERT INTO work VALUES (?, ?, ?)', (CMD2, 'op-2', 'after'))
                        except D.StoreError:
                            if not swallow:
                                raise
                        self.assertEqual(self.refused(lambda: tx.append(event())), 'transaction_aborted')
                self.assertEqual(str(caught.exception), 'transaction_aborted')
                self.assertEqual(committed(store, 'SELECT * FROM work'), [], 'nothing was written in autocommit')
        with store.transaction() as tx:
            tx.append(event())

    def test_a_commit_that_raises_is_outcome_unknown_and_fails_the_store(self) -> None:
        store = self.make()
        real = store._conn
        store._conn = FailingConnection(real, 'COMMIT', io_error())
        with self.assertRaises(D.CommitUnknown) as caught, self.assertLogs(D.LOG, 'ERROR'):
            with store.transaction() as tx:
                tx.append(event())
        self.assertEqual(str(caught.exception), 'commit_outcome_unknown')
        store._conn = real
        self.assertEqual(self.refused(lambda: store.transaction().__enter__()), 'store_failed')
        self.assertEqual(self.refused(lambda: store.register_tables('queue', 1, QUEUE)), 'store_failed')
        store.close()

    def test_an_io_error_fails_the_store_even_when_the_block_swallows_it(self) -> None:
        store = self.make()
        store._conn = FailingConnection(store._conn, 'INSERT INTO work', io_error())
        with self.assertRaises(D.StoreError) as caught, self.assertLogs(D.LOG, 'ERROR'):
            with store.transaction() as tx:
                with contextlib.suppress(sqlite3.OperationalError):
                    tx.execute('INSERT INTO work VALUES (?, ?, ?)', (CMD, 'op-1', 'pending'))
                self.assertEqual(self.refused(lambda: tx.append(event())), 'store_failed')
        self.assertEqual(str(caught.exception), 'store_failed')
        self.assertEqual(committed(store, 'SELECT * FROM work'), [])
        self.assertEqual(self.refused(lambda: store.transaction().__enter__()), 'store_failed')


class FakeOnly:
    @staticmethod
    def other_store(case) -> D.FakeStore:
        other = D.FakeStore()
        case.addCleanup(other.close)
        return other


class FakeStoreTest(Cases, unittest.TestCase):
    def make(self, declare=True) -> D.FakeStore:
        store = D.FakeStore()
        self.addCleanup(lambda: store.open_transactions or store.close())
        if declare:
            store.register_tables('sessions', 1, WORK)
        return store

    def test_the_fake_says_what_it_committed_and_nothing_is_written_through_that(self) -> None:
        store = self.make()
        with store.transaction() as tx:
            tx.receipt_put('t1', 'session.turn', CMD, digest('a'), {'code': 'ok'})
            tx.execute('INSERT INTO work VALUES (?, ?, ?)', (CMD, 'op-1', 'pending'))
            tx.append(event())
        self.assertEqual(store.receipts, {('t1', 'session.turn', CMD): (digest('a'), {'code': 'ok'})})
        self.assertEqual(store.rows, {('work', CMD): {'operation_id': 'op-1', 'state': 'pending'}})
        self.assertEqual([(e['seq'], e['type']) for e in store.events], [(1, 'result')])
        self.assertEqual((store.commits, store.rollbacks, store.open_transactions, store.closed, store.epoch), (1, 0, 0, False, 1))
        store.receipts[('t', 'm', 'c')] = None
        store.rows[('work', 'x')] = {}
        store.events.append({})
        self.assertEqual((len(store.receipts), len(store.rows), len(store.events)), (1, 1, 1))

    def test_fail_commit_fails_before_anything_is_published_and_the_store_goes_on(self) -> None:
        store = self.make()
        seen = []
        store.subscribe(seen.append)
        store.fail_commit = True
        with self.assertRaises(OSError):
            with store.transaction() as tx:
                tx.append(event())
                tx.receipt_put('t1', 'session.turn', CMD, digest('a'), {'code': 'ok'})
        self.assertEqual((store.events, store.receipts, seen, store.commits, store.rollbacks), ([], {}, [], 0, 1))
        store.fail_commit = False
        with store.transaction() as tx:
            tx.append(event())
        self.assertEqual((len(store.events), len(seen)), (1, 1))

    def test_a_close_is_logged_once_for_a_composition_that_watches_the_order(self) -> None:
        log = []
        store = D.FakeStore(log=log)
        store.close()
        store.close()
        self.assertEqual(log, [('close', 'store', None)])

    def test_the_fake_keeps_no_durability_and_says_so(self) -> None:
        store = self.make()
        self.assertEqual(store._conn.execute('PRAGMA synchronous').fetchone()[0], 0)
        self.assertIsNone(store._lock_fd)


@unittest.skipIf(D.fcntl is None, 'the real store needs POSIX file locks; the daemon runs on Linux')
class RealStoreTest(Cases, unittest.TestCase):
    def folder(self) -> str:
        folder = tempfile.mkdtemp(prefix='events-store-test-')
        self.addCleanup(shutil.rmtree, folder, True)
        return folder

    def make(self, declare=True, path=None) -> D.EventStore:
        store = D.EventStore(path or os.path.join(self.folder(), 'events.sqlite3'))
        self.addCleanup(lambda: store._open_count or store.close())
        if declare:
            store.register_tables('sessions', 1, WORK)
        return store

    def test_the_settings_the_durability_rests_on_took_effect(self) -> None:
        store = self.make()
        conn = store._conn
        self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        self.assertEqual(conn.execute('PRAGMA synchronous').fetchone()[0], 2, 'FULL')
        self.assertEqual(conn.execute('PRAGMA foreign_keys').fetchone()[0], 1)
        self.assertEqual(conn.execute('PRAGMA trusted_schema').fetchone()[0], 0)
        self.assertEqual(conn.execute('PRAGMA application_id').fetchone()[0], D.APPLICATION_ID)
        self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], D.SCHEMA_VERSION)
        self.assertEqual(os.stat(store.path).st_mode & 0o777, 0o600)

    def test_the_epoch_rises_by_one_on_every_open_and_the_log_goes_on(self) -> None:
        path = os.path.join(self.folder(), 'events.sqlite3')
        first = D.EventStore(path)
        with first.transaction() as tx:
            tx.append(event())
        self.assertEqual(first.epoch, 1)
        first.close()
        second = D.EventStore(path)
        self.addCleanup(second.close)
        self.assertEqual(second.epoch, 2)
        with second.transaction() as tx:
            self.assertEqual(tx.append(event()), 2)

    def test_one_process_writes_the_file(self) -> None:
        path = os.path.join(self.folder(), 'events.sqlite3')
        first = D.EventStore(path)
        self.assertEqual(self.refused(lambda: D.EventStore(path)), 'store_in_use')
        first.close()
        D.EventStore(path).close()

    def test_a_file_that_is_not_this_store_is_refused(self) -> None:
        folder = self.folder()
        other = os.path.join(folder, 'registry.sqlite3')
        with contextlib.closing(sqlite3.connect(other)) as conn:
            conn.execute('CREATE TABLE runs (run_id TEXT)')
            conn.commit()
        self.assertEqual(self.refused(lambda: D.EventStore(other)), 'not_an_event_store')
        with contextlib.closing(sqlite3.connect(other)) as conn:
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'delete', 'a refused file is not changed')
        junk = pathlib.Path(folder, 'junk.sqlite3')
        junk.write_bytes(b'this is not a database file at all, not even close' * 100)
        self.assertEqual(self.refused(lambda: D.EventStore(junk)), 'not_an_event_store')
        link = os.path.join(folder, 'link.sqlite3')
        os.symlink(other, link)
        self.assertEqual(self.refused(lambda: D.EventStore(link)), 'store_path_not_a_regular_file')

    def test_a_lane_declares_again_after_a_restart_and_a_changed_declaration_is_refused(self) -> None:
        path = os.path.join(self.folder(), 'events.sqlite3')
        first = D.EventStore(path)
        first.register_tables('sessions', 1, WORK)
        first.register_tables('queue', 1, QUEUE)
        with first.transaction() as tx:
            tx.execute('INSERT INTO work VALUES (?, ?, ?)', (CMD, 'op-1', 'pending'))
        first.close()
        second = D.EventStore(path)
        self.addCleanup(second.close)
        with self.assertRaises(sqlite3.DatabaseError):
            with second.transaction() as tx:
                tx.execute('SELECT * FROM work')            # not reachable until declared in this process
        second.register_tables('sessions', 1, WORK)
        self.assertEqual(self.refused(lambda: second.register_tables('queue', 2, QUEUE)), 'declaration_changed')
        self.assertEqual(self.refused(lambda: second.register_tables('queue', 1, QUEUE + ' ')), 'declaration_changed')
        with second.transaction() as tx:
            self.assertEqual(tx.execute('SELECT * FROM work'), [(CMD, 'op-1', 'pending')])

    def test_a_damaged_file_is_refused_at_open(self) -> None:
        path = os.path.join(self.folder(), 'events.sqlite3')
        store = D.EventStore(path)
        with store.transaction() as tx:
            for n in range(200):
                tx.append(event(payload={'n': n, 'text': 'x' * 500}))
        store.close()
        with contextlib.closing(sqlite3.connect(path)) as conn:
            page_size = conn.execute('PRAGMA page_size').fetchone()[0]
            pages = conn.execute('PRAGMA page_count').fetchone()[0]
        self.assertGreater(pages, 8)
        with open(path, 'r+b') as raw:                     # a page in the middle of the log, overwritten
            raw.seek(page_size * (pages // 2) + 16)
            raw.write(b'\xff' * (page_size - 32))
        self.assertEqual(self.refused(lambda: D.EventStore(path)), 'store_corrupt')

    def test_the_log_and_the_receipts_are_append_only_in_the_file(self) -> None:
        store = self.make()
        with store.transaction() as tx:
            tx.append(event())
            tx.receipt_put('t1', 'session.turn', CMD, digest('a'), {'code': 'ok'})
        with contextlib.closing(sqlite3.connect(store.path, isolation_level=None)) as conn:
            for statement in ("UPDATE event_log SET event = '{}'", 'DELETE FROM event_log',
                              "UPDATE receipts SET answer = '{}'", 'DELETE FROM receipts'):
                with self.subTest(statement=statement):
                    with self.assertRaisesRegex(sqlite3.DatabaseError, 'append_only'):
                        conn.execute(statement)


class ModuleTest(unittest.TestCase):
    def test_the_helper_imports_nothing_that_could_take_a_registry_daemon_or_run_lock(self) -> None:
        source = pathlib.Path(D.__file__).read_text(encoding='utf-8')
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, 'no relative import')
                imported.add(node.module)
        self.assertEqual(imported, {'__future__', 'collections', 'contextlib', 'hashlib', 'json', 'logging', 'os', 're',
                                    'shutil', 'sqlite3', 'stat', 'tempfile', 'threading', 'time', 'weakref', 'typing',
                                    'fcntl'})

    def test_the_lock_order_is_published_in_the_module(self) -> None:
        text = D.__doc__
        self.assertIn('LOCK ORDER', text)
        self.assertIn('NEVER taken while a run registry lock', text)
        self.assertIn('None of those is taken while the helper\'s lock is held', text)

    def test_the_source_is_ascii(self) -> None:
        for path in pathlib.Path(D.__file__).parent.rglob('*.py'):
            with self.subTest(path=path.name):
                path.read_bytes().decode('ascii')


if __name__ == '__main__':
    unittest.main()
