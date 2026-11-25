"""Transaction ownership and interruptions in events/db.py: the sweeps' triage of Nov 16, Draft 1 (EV1, EV2, EV6, EV3).

The four red tests of the triage (``EVENTS-red-tests.patch``, its ``TransactionTests`` and ``test_EV3_*``) are here with
their assertions as written. They run on this module's store (each case runs on the helper's fake and on the real
store), EV6's proxy is this module's ``ConnProxy``, and EV3 also asserts that the failure is logged. Around them, the
same promise is held at its other points: an interruption right after BEGIN ran, an owner that ends its transaction
after a foreign exit was refused, SystemExit at COMMIT, an interruption at a declaration's COMMIT, an interruption
after a declaration's BEGIN, an interruption during a ROLLBACK, a fatal error on the retried ROLLBACK, SystemExit on
that rollback and after a declaration's BEGIN, and an interruption that a subscriber raises.
An interruption here is an exception that is not an ``Exception``: KeyboardInterrupt, SystemExit.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from recursion_core.events import db as events_db
from recursion_core.events.db import EventStore, FakeStore, StoreError

BOUND_S = 30.0          # only ends a wait that would otherwise never end
POSIX = events_db.fcntl is not None


def event(kind='result', log='run:run-1', payload=None, **extra):
    out = {'session_id': log, 'run_id': 'run-1', 'turn_id': None, 'type': kind, 'source': 'core',
           'trust': 'trusted', 'payload': {'code': 'ok', 'detail': None} if payload is None else payload}
    out.update(extra)
    return out


class ConnProxy:
    """The store's connection, with one statement made to raise ``exc`` instead of running: every time, or only its
    first ``times`` times."""

    def __init__(self, real, statement: str, exc: BaseException, times: int | None = None) -> None:
        self._real, self._statement, self._exc, self._left = real, statement, exc, times

    def __getattr__(self, name):
        return getattr(self._real, name)

    def execute(self, sql, *args):
        if sql == self._statement and (self._left is None or self._left > 0):
            if self._left is not None:
                self._left -= 1
            raise self._exc
        return self._real.execute(sql, *args)


class Case(unittest.TestCase):
    def new_store(self):
        store = FakeStore()
        self.addCleanup(lambda: store._open_count or store.close())
        return store

    def assertUsable(self, store) -> None:
        """No transaction is left: the count is 0, the lock is free, the connection holds nothing open, and a
        transaction commits."""
        self.assertEqual(store._open_count, 0)
        self.assertFalse(store._lock.locked())
        self.assertFalse(store._conn.in_transaction)
        self.assertFalse(events_db.in_transaction())
        before = len(store.events)
        with store.transaction() as tx:
            tx.append(event())
        self.assertEqual(len(store.events), before + 1)


class TransactionTests(Case):
    def test_EV1_interruption_after_begin_leaves_the_store_usable(self) -> None:
        # The triage's red test (s2b-events-db-sol F01, s2b-events-db-astra F01), on this case's store.
        store = self.new_store()
        real = events_db.Transaction
        calls = []

        def interrupted(owner):
            if not calls:
                calls.append(1)
                raise KeyboardInterrupt  # arrives right after BEGIN IMMEDIATE succeeded
            return real(owner)

        with mock.patch.object(events_db, 'Transaction', interrupted):
            with self.assertRaises(KeyboardInterrupt):
                with store.transaction():
                    pass
            with store.transaction() as tx:  # sqlite3.OperationalError on master
                tx.append(event())
        self.assertEqual(len(store.events), 1)

    def test_an_interruption_as_begin_returns_is_rolled_back_too(self) -> None:
        # The same promise at the other point after BEGIN: the statement ran, then the interruption came.
        store = self.new_store()
        real, calls = store._statement, []

        def begun_then_interrupted(sql):
            real(sql)
            if sql == 'BEGIN IMMEDIATE' and not calls:
                calls.append(1)
                raise KeyboardInterrupt
        store._statement = begun_then_interrupted
        try:
            with self.assertRaises(KeyboardInterrupt):
                with store.transaction():
                    pass
        finally:
            del store._statement
        self.assertEqual(store.rollbacks, 0, 'not one of the counted rollbacks: no transaction was handed out')
        self.assertUsable(store)

    def test_an_interruption_as_the_transaction_is_marked_held_is_rolled_back_too(self) -> None:
        # The last point of the opening: the thread's mark that it holds a transaction is set, then the interruption.
        store = self.new_store()

        class Held(threading.local):
            armed = True

            def __setattr__(self, name, value):
                super().__setattr__(name, value)
                if name == 'store' and value is not None and Held.armed:
                    Held.armed = False
                    raise KeyboardInterrupt

        with mock.patch.object(events_db, '_HELD', Held()):
            with self.assertRaises(KeyboardInterrupt):
                with store.transaction():
                    pass
            self.assertFalse(events_db.in_transaction(), 'the mark is taken back')
            self.assertUsable(store)

    def test_an_interruption_after_a_declarations_begin_leaves_the_store_usable(self) -> None:
        # DeepSeek r1 on #441, FINDING 1 (db.py:823): the declaration's BEGIN was outside its try.
        store = self.new_store()
        real, calls = store._statement, []

        def begun_then_interrupted(sql):
            real(sql)
            if sql == 'BEGIN IMMEDIATE' and not calls:
                calls.append(1)
                raise KeyboardInterrupt
        store._statement = begun_then_interrupted
        try:
            with self.assertRaises(KeyboardInterrupt):
                store.register_tables('triage', 1, 'CREATE TABLE triage_items (id INTEGER PRIMARY KEY)')
        finally:
            del store._statement
        self.assertIsNone(store._failed)
        self.assertUsable(store)
        store.register_tables('triage', 1, 'CREATE TABLE triage_items (id INTEGER PRIMARY KEY)')   # nothing was kept

    def test_an_interruption_during_a_rollback_leaves_no_transaction_open(self) -> None:
        # DeepSeek r1 on #441, FINDING 2 (probe P10): the body raises, and the rollback is interrupted once.
        store = self.new_store()
        real = store._conn
        store._conn = ConnProxy(real, 'ROLLBACK', KeyboardInterrupt(), times=1)
        try:
            with self.assertRaises(KeyboardInterrupt):
                with store.transaction() as tx:
                    tx.append(event())
                    raise ValueError('the body fails')
        finally:
            store._conn = real
        self.assertIsNone(store._failed, 'the second ROLLBACK ran')
        self.assertEqual(store.events, [])
        self.assertUsable(store)

    def test_an_interruption_during_the_openings_rollback_still_gives_the_count_back(self) -> None:
        # DeepSeek r1 on #441, FINDING 2 (probe P2): interrupted after BEGIN, then again during the rollback.
        store = self.new_store()
        real_statement, real_conn, calls = store._statement, store._conn, []

        def begun_then_interrupted(sql):
            real_statement(sql)
            if sql == 'BEGIN IMMEDIATE' and not calls:
                calls.append(1)
                raise KeyboardInterrupt
        store._statement = begun_then_interrupted
        store._conn = ConnProxy(real_conn, 'ROLLBACK', KeyboardInterrupt(), times=1)
        try:
            with self.assertRaises(KeyboardInterrupt):
                with store.transaction():
                    pass
        finally:
            del store._statement
            store._conn = real_conn
        self.assertUsable(store)

    def test_a_rollback_that_cannot_be_run_again_fails_the_store(self) -> None:
        # FINDING 2's other end: every ROLLBACK is interrupted, so the connection still holds the transaction.
        store = self.new_store()
        real = store._conn
        store._conn = ConnProxy(real, 'ROLLBACK', KeyboardInterrupt())
        try:
            with self.assertRaises(KeyboardInterrupt):
                with store.transaction() as tx:
                    tx.append(event())
                    raise ValueError('the body fails')
        finally:
            store._conn = real
        self.assertEqual(store._failed, 'rollback_failed')
        self.assertEqual(store._open_count, 0)
        self.assertFalse(store._lock.locked())
        with self.assertRaisesRegex(StoreError, 'store_failed'):
            store.transaction().__enter__()             # refused by name, not a raw sqlite3 error
        real.execute('ROLLBACK')                        # for the cleanup: the test's own connection state

    def test_a_fatal_error_on_the_retried_rollback_fails_the_store(self) -> None:
        # Grok r2 on #441, R1 (db.py _rollback): the retried ROLLBACK's fatal error was dropped once the
        # transaction had ended, and the next transaction committed.
        store = self.new_store()
        real = store._conn
        err = sqlite3.OperationalError('injected SQLITE_IOERR')
        err.sqlite_errorcode, err.sqlite_errorname = 10, 'SQLITE_IOERR'

        class Proxy:
            def __init__(self) -> None:
                self.n = 0

            def __getattr__(self, name):
                return getattr(real, name)

            def execute(self, sql, *args):
                if sql == 'ROLLBACK':
                    self.n += 1
                    if self.n == 1:
                        raise KeyboardInterrupt()
                    real.execute(sql, *args)
                    raise err
                return real.execute(sql, *args)

        store._conn = Proxy()
        try:
            with self.assertRaises(KeyboardInterrupt):
                with store.transaction() as tx:
                    tx.append(event())
                    raise ValueError('the body fails')
        finally:
            store._conn = real
        self.assertEqual(store.events, [])
        self.assertEqual(store._failed, 'rollback_failed')
        with self.assertRaisesRegex(StoreError, 'store_failed'):
            store.transaction().__enter__()

    def test_a_nonfatal_error_on_the_retried_rollback_leaves_the_store_usable(self) -> None:
        # The other half of R1: a code the page cache can be trusted after, once the transaction has ended
        # (SQLITE_FULL), does not fail the store. test_each_fatal_code already says that of a statement.
        store = self.new_store()
        real = store._conn
        err = sqlite3.OperationalError('injected SQLITE_FULL')
        err.sqlite_errorcode, err.sqlite_errorname = 13, 'SQLITE_FULL'

        class Proxy:
            def __init__(self) -> None:
                self.n = 0

            def __getattr__(self, name):
                return getattr(real, name)

            def execute(self, sql, *args):
                if sql == 'ROLLBACK':
                    self.n += 1
                    if self.n == 1:
                        raise KeyboardInterrupt()
                    real.execute(sql, *args)
                    raise err
                return real.execute(sql, *args)

        store._conn = Proxy()
        try:
            with self.assertRaises(KeyboardInterrupt):
                with store.transaction() as tx:
                    tx.append(event())
                    raise ValueError('the body fails')
        finally:
            store._conn = real
        self.assertIsNone(store._failed)
        self.assertEqual(store.events, [])
        self.assertUsable(store)

    def test_systemexit_during_a_rollback_leaves_no_transaction_open(self) -> None:
        # Grok r2 on #441, R2: the rollback handler catches BaseException, and SystemExit had no test.
        store = self.new_store()
        real = store._conn

        class Proxy:
            def __init__(self) -> None:
                self.n = 0

            def __getattr__(self, name):
                return getattr(real, name)

            def execute(self, sql, *args):
                if sql == 'ROLLBACK':
                    self.n += 1
                    if self.n == 1:
                        raise SystemExit(3)
                return real.execute(sql, *args)

        store._conn = Proxy()
        try:
            with self.assertRaises(SystemExit):
                with store.transaction() as tx:
                    tx.append(event())
                    raise ValueError('the body fails')
        finally:
            store._conn = real
        self.assertIsNone(store._failed)
        self.assertEqual(store.events, [])
        self.assertUsable(store)

    def test_systemexit_after_a_declarations_begin_leaves_the_store_usable(self) -> None:
        # Grok r2 on #441, R2: the declaration handler catches BaseException, and SystemExit had no test.
        store = self.new_store()
        real, calls = store._statement, []

        def begun(sql):
            real(sql)
            if sql == 'BEGIN IMMEDIATE' and not calls:
                calls.append(1)
                raise SystemExit(3)
        store._statement = begun
        try:
            with self.assertRaises(SystemExit):
                store.register_tables('triage', 1, 'CREATE TABLE triage_items (id INTEGER PRIMARY KEY)')
        finally:
            del store._statement
        self.assertIsNone(store._failed)
        self.assertUsable(store)
        store.register_tables('triage', 1, 'CREATE TABLE triage_items (id INTEGER PRIMARY KEY)')

    def test_EV2_another_thread_cannot_exit_the_owners_transaction(self) -> None:
        # The triage's red test (s2b-events-db-sol F02, s2b-events-db-astra F02; db.py:320).
        store = self.new_store()
        opening = store.transaction()
        tx = opening.__enter__()
        tx.append(event())
        outcome = []

        def foreign():
            try:
                opening.__exit__(None, None, None)
                outcome.append('committed')
            except StoreError as exc:
                outcome.append(str(exc))

        worker = threading.Thread(target=foreign)
        worker.start()
        worker.join(10)
        self.assertFalse(worker.is_alive())
        try:
            self.assertNotEqual(outcome, ['committed'])
            self.assertEqual(store.events, [])
        finally:
            if outcome != ['committed']:
                opening.__exit__(ValueError, ValueError('roll back'), None)
            else:
                events_db._HELD.store = None

    def test_after_a_foreign_exit_is_refused_the_owner_still_ends_its_transaction(self) -> None:
        store = self.new_store()
        opening = store.transaction()
        opening.__enter__().append(event())
        refused = []
        worker = threading.Thread(target=lambda: refused.append(self._exit_from_here(opening)))
        worker.start()
        worker.join(BOUND_S)
        try:
            self.assertEqual(refused, ['transaction_not_active_for_caller'])
            self.assertEqual(store._open_count, 1, 'the foreign exit changed nothing')
        except BaseException:
            if refused == ['committed']:
                events_db._HELD.store = None    # the foreign thread ended it; this thread's mark was left set
            else:
                opening.__exit__(ValueError, ValueError('roll back'), None)
            raise
        opening.__exit__(None, None, None)                  # the owner commits
        self.assertEqual(len(store.events), 1)
        self.assertUsable(store)

    @staticmethod
    def _exit_from_here(opening) -> str:
        try:
            opening.__exit__(None, None, None)
        except StoreError as exc:
            return str(exc)
        return 'committed'

    def test_EV6_keyboard_interrupt_at_commit_is_not_turned_into_commit_unknown(self) -> None:
        # The triage's red test (s6-exc F14, db.py:668-673), on this case's store.
        store = self.new_store()
        real = store._conn
        store._conn = ConnProxy(real, 'COMMIT', KeyboardInterrupt())
        try:
            with self.assertRaises(KeyboardInterrupt):
                with store.transaction() as tx:
                    tx.append(event())
        finally:
            store._conn = real

    def test_an_interruption_at_commit_fails_the_store_and_stays_itself(self) -> None:
        for interruption in (KeyboardInterrupt(), SystemExit(3)):
            with self.subTest(interruption=type(interruption).__name__):
                store = self.new_store()
                real = store._conn
                store._conn = ConnProxy(real, 'COMMIT', interruption)
                try:
                    with self.assertRaises(type(interruption)) as caught:
                        with store.transaction() as tx:
                            tx.append(event())
                finally:
                    store._conn = real
                self.assertIs(caught.exception, interruption)
                self.assertNotIsInstance(caught.exception, events_db.CommitUnknown)
                with self.assertRaisesRegex(StoreError, 'store_failed'):
                    store.transaction().__enter__()    # the commit may have happened: the store waits for a reopen
                self.assertEqual(store._failed, 'commit_outcome_unknown')
                self.assertFalse(store._lock.locked())

    def test_an_ordinary_error_at_commit_is_still_commit_unknown(self) -> None:
        store = self.new_store()
        real = store._conn
        store._conn = ConnProxy(real, 'COMMIT', OSError('disk'))
        try:
            with self.assertRaises(events_db.CommitUnknown):
                with store.transaction() as tx:
                    tx.append(event())
        finally:
            store._conn = real

    def test_an_interruption_at_a_declarations_commit_stays_itself(self) -> None:
        # The triage names the declaration's COMMIT too (db.py:837-842).
        store = self.new_store()
        real = store._conn
        store._conn = ConnProxy(real, 'COMMIT', KeyboardInterrupt())
        try:
            with self.assertRaises(KeyboardInterrupt):
                store.register_tables('triage', 1, 'CREATE TABLE triage_items (id INTEGER PRIMARY KEY)')
        finally:
            store._conn = real
        self.assertEqual(store._failed, 'commit_outcome_unknown')


class SubscriberTests(Case):
    def test_EV3_a_cancelled_subscriber_does_not_escape_or_drop_the_batch(self) -> None:
        # The triage's red test (s2b-events-db-sol F10, s2b-events-db-astra F03; db.py:719-722).
        store = self.new_store()
        seen = []

        def cancelled(events):
            raise asyncio.CancelledError('subscriber cancelled')

        unsubscribe = store.subscribe(cancelled)
        store.subscribe(lambda events: seen.extend(e['seq'] for e in events))
        try:
            with self.assertLogs(events_db.LOG, 'ERROR'):
                with store.transaction() as tx:
                    tx.append(event())
        except BaseException:  # noqa: BLE001 - the escape is part of what is measured
            escaped = True
        else:
            escaped = False
        unsubscribe()
        with store.transaction() as tx:
            tx.append(event())
        self.assertFalse(escaped)
        self.assertEqual(seen, [1, 2])

    def test_an_interruption_a_subscriber_raises_is_not_swallowed(self) -> None:
        store = self.new_store()
        seen = []
        unsubscribe = store.subscribe(lambda events: (_ for _ in ()).throw(KeyboardInterrupt()))
        store.subscribe(lambda events: seen.extend(e['seq'] for e in events))
        with self.assertRaises(KeyboardInterrupt):
            with store.transaction() as tx:
                tx.append(event())
        self.assertEqual(len(store.events), 1, 'the commit stands')
        self.assertFalse(store._publishing)
        unsubscribe()
        with store.transaction() as tx:
            tx.append(event())
        self.assertEqual(seen, [2], 'the interrupted batch is not delivered; the next one is')


class RealStoreMixin:
    def new_store(self):
        folder = tempfile.mkdtemp(prefix='events-interruptions-')
        self.addCleanup(shutil.rmtree, folder, True)
        store = RealStore(os.path.join(folder, 'events.sqlite3'))
        self.addCleanup(lambda: store._open_count or store.close())
        return store


class RealStore(EventStore):
    """The real helper with the fake's read-only views for the assertions."""

    _reader = FakeStore._reader
    _read = FakeStore._read
    events = FakeStore.events
    rollbacks = FakeStore.rollbacks


@unittest.skipUnless(POSIX, 'the real store needs flock')
class TransactionOnTheRealStoreTest(RealStoreMixin, TransactionTests):
    """Every case above, against the real store (WAL, synchronous FULL, its lock file)."""


@unittest.skipUnless(POSIX, 'the real store needs flock')
class SubscriberOnTheRealStoreTest(RealStoreMixin, SubscriberTests):
    """Every case above, against the real store."""


if __name__ == '__main__':
    unittest.main()
