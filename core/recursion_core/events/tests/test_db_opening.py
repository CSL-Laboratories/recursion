"""Opening a damaged store, the subscriber log and FakeStore.rows in events/db.py: the sweeps' triage of Nov 16,
Draft 2 (EV7, EV5, EV4, EV8).

The four red tests of the triage (``EVENTS-red-tests.patch``: ``OpeningTests``, ``test_EV4_*``, ``FakeStoreTests``) are
kept as they were written. Around them: a new path still makes a store, a store that existed keeps what it holds, an
empty file that exists before any opening is refused too, an epoch that is not a whole number is refused, the log of a
failed subscriber still says what failed and where, and a table name without a quote reads as before.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tempfile
import unittest

from recursion_core.events import db as events_db
from recursion_core.events.db import EventStore, FakeStore, StoreError

POSIX = events_db.fcntl is not None
MARKER = 'TRIAGE_SYNTHETIC_MARKER'


def event(kind='result', log='run:run-1', payload=None, **extra):
    out = {'session_id': log, 'run_id': 'run-1', 'turn_id': None, 'type': kind, 'source': 'core',
           'trust': 'trusted', 'payload': {} if payload is None else payload}
    out.update(extra)
    return out


def _path(test):
    folder = tempfile.mkdtemp(prefix='triage-events-')
    test.addCleanup(shutil.rmtree, folder, True)
    return os.path.join(folder, 'events.sqlite3')


class SubscriberLogTests(unittest.TestCase):
    def test_EV4_subscriber_failure_log_does_not_carry_the_exception_text(self) -> None:
        # The triage's red test (s2b-events-db-sol F04, s2b-events-db-astra F04).
        store = FakeStore()
        self.addCleanup(store.close)

        def failing(events):
            raise ValueError(events[0]['payload']['private_value'])

        store.subscribe(failing)
        with self.assertLogs(events_db.LOG.name, level='ERROR') as logs:
            with store.transaction() as tx:
                tx.append(event(payload={'private_value': MARKER}))
        text = '\n'.join(logs.output)
        for record in logs.records:
            if record.exc_info:
                text += '\n' + logging.Formatter().formatException(record.exc_info)
        self.assertNotIn(MARKER, text)

    def test_the_log_still_says_what_failed_and_where(self) -> None:
        store = FakeStore()
        self.addCleanup(store.close)

        def failing_subscriber(events):
            raise KeyError(events[0]['payload']['private_value'])

        store.subscribe(failing_subscriber)
        with self.assertLogs(events_db.LOG.name, level='ERROR') as logs:
            with store.transaction() as tx:
                tx.append(event(payload={'private_value': MARKER}))
        line, = logs.output
        self.assertIn('KeyError', line)
        self.assertIn('in failing_subscriber', line)
        self.assertIn(os.path.basename(__file__), line)
        self.assertNotIn(MARKER, line)
        self.assertFalse(any(record.exc_info for record in logs.records), 'no traceback carries the text either')


@unittest.skipUnless(POSIX, 'the real store needs flock')
class OpeningTests(unittest.TestCase):
    def test_EV5_missing_epoch_row_is_a_store_refusal(self) -> None:
        # The triage's red test (s2b-events-db-sol F03).
        path = _path(self)
        EventStore(path).close()
        db = sqlite3.connect(path)
        db.execute("DELETE FROM store_meta WHERE key = 'epoch'")
        db.commit()
        db.close()
        with self.assertRaises(StoreError):
            EventStore(path).close()

    def test_an_epoch_that_is_not_a_whole_number_is_refused_and_nothing_is_left_open(self) -> None:
        path = _path(self)
        EventStore(path).close()
        db = sqlite3.connect(path)
        db.execute("UPDATE store_meta SET value = 'x' WHERE key = 'epoch'")
        db.commit()
        db.close()
        with self.assertRaisesRegex(StoreError, 'store_epoch_missing'):
            EventStore(path)
        db = sqlite3.connect(path)
        self.assertEqual(db.execute("SELECT value FROM store_meta WHERE key = 'epoch'").fetchone(), ('x',),
                         'the refused opening changed nothing')
        db.close()
        with self.assertRaisesRegex(StoreError, 'store_epoch_missing'):
            EventStore(path)                            # the lock file was given up: the same refusal, not store_in_use

    def test_EV7_existing_store_emptied_to_zero_bytes_is_not_silently_reset(self) -> None:
        # The triage's red test (s6-disk F8).
        path = _path(self)
        store = EventStore(path)
        with store.transaction() as tx:
            tx.receipt_put('tenant', 'run.start', 'command-0001', '0' * 64, {'ok': True})
        store.close()
        for suffix in ('-wal', '-shm'):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)
        with open(path, 'r+b') as fh:
            fh.truncate(0)
        with self.assertRaises(StoreError):
            EventStore(path).close()

    def test_an_emptied_store_is_refused_by_name_and_left_as_it_is(self) -> None:
        path = _path(self)
        EventStore(path).close()
        for suffix in ('-wal', '-shm'):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)
        with open(path, 'r+b') as fh:
            fh.truncate(0)
        with self.assertRaisesRegex(StoreError, 'store_empty'):
            EventStore(path)
        self.assertEqual(os.path.getsize(path), 0, 'nothing was made in it')
        os.remove(path)
        EventStore(path).close()                        # removed, a new store is made at the same path

    def test_an_empty_file_that_exists_before_any_opening_is_refused_too(self) -> None:
        # The cost of the rule, said in the module: an empty file the store did not make is not made a store.
        path = _path(self)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        with self.assertRaisesRegex(StoreError, 'store_empty'):
            EventStore(path)

    def test_a_new_path_makes_a_store_and_a_reopened_store_keeps_what_it_holds(self) -> None:
        path = _path(self)
        store = EventStore(path)
        self.assertEqual(store.epoch, 1)
        with store.transaction() as tx:
            tx.receipt_put('tenant', 'run.start', 'command-0001', '0' * 64, {'ok': True})
        store.close()
        store = EventStore(path)
        self.addCleanup(store.close)
        self.assertEqual(store.epoch, 2)
        with store.transaction() as tx:
            self.assertEqual(tx.receipt_get('tenant', 'run.start', 'command-0001'), ('0' * 64, {'ok': True}))


class FakeStoreTests(unittest.TestCase):
    def test_EV8_rows_reads_a_declared_table_whose_name_holds_a_quote(self) -> None:
        # The triage's red test (s3b-events-astra F13, s3b-events-sol F8, s2b-events-db-sol F06, astra F05).
        store = FakeStore()
        self.addCleanup(store.close)
        store.register_tables('probe', 1, 'CREATE TABLE "probe_a""b" (id INTEGER PRIMARY KEY, value TEXT)')
        with store.transaction() as tx:
            tx.execute('INSERT INTO "probe_a""b" (id, value) VALUES (1, \'one\')')
        self.assertEqual(store.rows, {('probe_a"b', 1): {'value': 'one'}})

    def test_a_table_name_without_a_quote_reads_as_before(self) -> None:
        store = FakeStore()
        self.addCleanup(store.close)
        store.register_tables('probe', 1, 'CREATE TABLE probe_items (id INTEGER PRIMARY KEY, value TEXT, n INTEGER)')
        with store.transaction() as tx:
            tx.execute("INSERT INTO probe_items (id, value, n) VALUES (1, 'one', 1), (2, 'two', 2)")
        self.assertEqual(store.rows, {('probe_items', 1): {'value': 'one', 'n': 1},
                                      ('probe_items', 2): {'value': 'two', 'n': 2}})


if __name__ == '__main__':
    unittest.main()
