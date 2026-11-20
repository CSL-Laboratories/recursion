"""Sixteen threads append and read one event store for a bounded time (events/db.py).

Public calls only: ``EventStore``, ``register_tables``, ``subscribe``, ``transaction``,
``append``, ``execute``, ``receipt_put``, ``receipt_get``, ``epoch`` and ``close``.
The store lives in a directory from ``tempfile``. There is no public call that
re-reads ``event_log`` (``execute`` refuses the helper's tables) and no public
call that returns ``quick_check`` by itself. Opening a store runs that check and
refuses the file when it is not ok, so a second ``EventStore`` on the same file
is the check. Each append is also written, in the same transaction, as a lane row
and a receipt, which are the public reads of that append, and ``subscribe``
receives the stored envelope.

The clock bound only stops new work. The join bound only reports a worker that
never returned. Neither decides a pass. The assertions are the state afterwards:
the store opens again, the log's seq is 1..N with no gap, and every append can
be read back.

This file is ASCII on purpose: events/tests/test_db.py reads every .py file under events/.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import threading
import time
import traceback
import unittest

from recursion_core.events import db as D


THREADS = 16
# Stops new iterations. Not an assertion: a run is not judged by duration or by how many appends it made.
WORK_S = 2.0
# A worker that has not returned by this bound is still inside the store. It does not decide a pass.
JOIN_S = 20.0
LOG_ID = 'run:stress'
RUN_ID = 'stress'
TENANT = 'stress'
METHOD = 'stress.append'
DDL = 'CREATE TABLE stress_rows (token TEXT PRIMARY KEY, seq INTEGER NOT NULL)'
DIGEST = hashlib.sha256(b'events-thread-stress').hexdigest()


def command_id(token: str) -> str:
    return 'cmd-' + token


def event(token: str) -> dict:
    return {'session_id': LOG_ID, 'run_id': RUN_ID, 'turn_id': None, 'type': 'result', 'source': 'core',
            'trust': 'trusted', 'payload': {'token': token}}


def worker(index: int, store: D.EventStore, stop_at: float, stop: threading.Event, out: list,
           errors: list, error_lock: threading.Lock) -> None:
    """Append and read until the bound, recording every commit this thread made."""
    wrote: list = []
    n = 0
    try:
        while not stop.is_set():
            n += 1
            token = 't%02d-%06d' % (index, n)
            with store.transaction() as tx:
                if wrote:
                    prev_token, prev_seq = wrote[-1]
                    prev = tx.receipt_get(TENANT, METHOD, command_id(prev_token))
                    rows = tx.execute('SELECT seq FROM stress_rows WHERE token = ?', (prev_token,))
                    if prev is None or prev.answer != {'token': prev_token, 'seq': prev_seq} or rows != [(prev_seq,)]:
                        raise AssertionError('committed append not readable: %r %r' % (prev, rows))
                (count, prior), = tx.execute('SELECT COUNT(*), COALESCE(MAX(seq), 0) FROM stress_rows')
                if count != prior:
                    raise AssertionError('sequence already has a gap or a duplicate: count=%s max=%s' % (count, prior))
                seq = tx.append(event(token))
                if seq != prior + 1:
                    raise AssertionError('append seq %s is not %s' % (seq, prior + 1))
                tx.execute('INSERT INTO stress_rows (token, seq) VALUES (?, ?)', (token, seq))
                answer = {'token': token, 'seq': seq}
                tx.receipt_put(TENANT, METHOD, command_id(token), DIGEST, answer)
                got = tx.receipt_get(TENANT, METHOD, command_id(token))
                if got != D.Receipt(DIGEST, answer):
                    raise AssertionError('receipt not readable in its transaction: %r' % (got,))
            wrote.append((token, seq))
            if time.monotonic() >= stop_at:
                break
        out[index] = wrote
    except Exception:
        stop.set()
        with error_lock:
            errors.append('thread %d after %d committed appends\n%s' % (index, len(wrote), traceback.format_exc()))


def stacks(workers: list) -> str:
    frames = sys._current_frames()
    parts = []
    for thread in workers:
        parts.append('--- %s ident=%s alive=%s ---' % (thread.name, thread.ident, thread.is_alive()))
        frame = frames.get(thread.ident) if thread.ident is not None else None
        if frame is not None:
            parts.append(''.join(traceback.format_stack(frame)))
    return '\n'.join(parts)


@unittest.skipUnless(D.fcntl is not None, 'the real store needs a POSIX file lock')
class ThreadStressTest(unittest.TestCase):
    def test_sixteen_threads_append_and_read_then_the_log_is_gapless_and_every_append_is_readable(self) -> None:
        folder = tempfile.TemporaryDirectory(prefix='events-thread-stress-')
        self.addCleanup(folder.cleanup)
        path = os.path.join(folder.name, 'events.sqlite3')
        store = D.EventStore(path)
        try:
            self.assertIsInstance(store.epoch, int)
            opened = store.epoch
            store.register_tables(TENANT, 1, DDL)
            seen: list = []
            seen_lock = threading.Lock()

            def collect(events) -> None:
                with seen_lock:
                    seen.extend(events)

            store.subscribe(collect)
            out: list = [None] * THREADS
            errors: list = []
            error_lock = threading.Lock()
            stop = threading.Event()
            stop_at = time.monotonic() + WORK_S
            # daemon: a worker that never returns must not keep the process up after the failure is reported.
            workers = [threading.Thread(target=worker, name='stress-%02d' % index, daemon=True,
                                        args=(index, store, stop_at, stop, out, errors, error_lock))
                       for index in range(THREADS)]
            for thread in workers:
                thread.start()
            for thread in workers:
                thread.join(JOIN_S)
            alive = [thread for thread in workers if thread.is_alive()]
            if alive:
                self.fail('a worker was still inside the store\n%s\ninterpreter: %s\n_xoptions: %r'
                          % (stacks(workers), sys.version, sys._xoptions))
            if errors:
                store.close()
                self.fail(''.join(errors) + '\ninterpreter: %s\n_xoptions: %r' % (sys.version, sys._xoptions))
            appended = []
            for index, part in enumerate(out):
                self.assertTrue(part, 'thread %d recorded no append' % index)
                appended.extend(part)
            with seen_lock:
                delivered = list(seen)
            store.close()

            try:
                reopened = D.EventStore(path)
            except D.StoreError as exc:
                self.fail('the store refused to open again (%s); opening runs quick_check\ninterpreter: %s'
                          % (exc, sys.version))
            try:
                self.assertEqual(reopened.epoch, opened + 1)
                reopened.register_tables(TENANT, 1, DDL)
                with reopened.transaction() as tx:
                    rows = list(tx.execute('SELECT token, seq FROM stress_rows ORDER BY seq'))
                    receipts = [tx.receipt_get(TENANT, METHOD, command_id(token)) for token, _seq in rows]
            finally:
                reopened.close()
        finally:
            if store is not None:
                try:
                    store.close()
                except D.StoreError:
                    pass

        expected = sorted(appended, key=lambda item: item[1])
        seqs = [seq for _token, seq in rows]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)), 'the sequence has a gap or a duplicate')
        self.assertEqual(rows, expected, 'a committed append is missing or stored under another seq')
        self.assertEqual(receipts, [D.Receipt(DIGEST, {'token': token, 'seq': seq}) for token, seq in expected])
        self.assertEqual([(item['payload']['token'], item['seq']) for item in delivered], expected,
                         'an append was not delivered to the subscriber')
        self.assertEqual([item['seq'] for item in delivered], seqs)
        self.assertEqual({item['session_id'] for item in delivered}, {LOG_ID})
        self.assertEqual({item['type'] for item in delivered}, {'result'})
        self.assertTrue(all(isinstance(item['time'], str) and item['time'] for item in delivered))
        if os.environ.get('EVENTS_STRESS_REPORT'):
            sys.stderr.write('\n[thread-stress] appends %d\n' % len(appended))


if __name__ == '__main__':
    unittest.main()
