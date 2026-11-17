"""The change reader (events/changes.py) against the daemon's real Registry and SPINE's real ChangeMarker.

The reader is driven one ``step()`` at a time with a clock the test moves, so nothing here waits on time
except to check that something has NOT happened yet. Every case runs twice: on the helper's fake (its own code,
durability off) and on the real event store in the test's folder (``RealStore``: WAL, synchronous FULL, its
lock file), the paused-consumer cases among them. What the helper itself guarantees under a crash is in
test_db.py and test_durability.py; the reader's own crash points, by kill -9, are in test_reader_crash.py.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from recursion_core.events import changes as C
from recursion_core.events import db as D

try:
    from recursion_core import daemon as daemon_module
    from recursion_core.control import hooks as H
    from recursion_core.daemon import Registry
    POSIX = D.fcntl is not None
except Exception:                                   # noqa: BLE001 - the daemon is Linux only
    POSIX = False

BOUND_S = 30.0          # only ends a wait that would otherwise never end; it never decides a pass
NOT_YET_S = 0.2         # used only to check that something has NOT happened yet
RUN, RUN2, RUN3 = 'run-aaaa', 'run-bbbb', 'run-cccc'


def record(text='a', **launch):
    """A control document as the registry stores it: JSON text. Only what the reader reads is shaped."""
    doc = {'run_id': RUN, 'note': text}
    if launch:
        doc['launch'] = {'route_decision': None, 'session_id': None, 'turn_id': None, **launch}
    return json.dumps(doc)


class OddMarker:
    """SPINE's marker, handing back the values in ``extra`` with its next batch (DeepSeek r3, L7)."""

    def __init__(self, inner) -> None:
        self.inner, self.extra = inner, []

    @property
    def poll_interval_s(self):
        return self.inner.poll_interval_s

    def take(self):
        ids, dropped = self.inner.take()
        extra, self.extra = self.extra, []
        return [*extra, *ids], dropped


@unittest.skipUnless(POSIX, 'the daemon registry is Linux only')
class ChangeReaderTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.mkdtemp(prefix='events-changes-')
        self.addCleanup(shutil.rmtree, folder, True)
        self.folder = Path(folder)
        self.registry = Registry(self.folder / 'state')
        self.addCleanup(self.registry_close)
        self.marker = H.ChangeMarker()
        self.registry.mark_changed = self.marker.mark
        self.store = self.new_store()
        self.now = [0.0]
        self.reader = self.new_reader()

    def registry_close(self) -> None:
        with contextlib.suppress(Exception):
            self.registry.close()

    def new_store(self) -> D.FakeStore:
        store = D.FakeStore()
        store.register_tables(C.LANE, C.LANE_VERSION, C.LANE_DDL)
        self.addCleanup(lambda: store.open_transactions or store.close())
        return store

    def new_reader(self, **options) -> C.ChangeReader:
        reader = C.ChangeReader(self.marker, monotonic=lambda: self.now[0], **options)
        reader.bind(self.registry, self.store)
        return reader

    def drain(self, reader=None) -> None:
        """Steps until no scan is in progress and nothing is pending or held. A wait the reader is in (after a
        refusal) is passed by moving the test's clock to the wait's end, never further."""
        reader = reader or self.reader
        for _ in range(1000):
            if reader.step():
                continue
            if (not reader._pending and getattr(reader, '_held', None) is None and not reader._scanning
                    and getattr(reader, '_wait_until', None) is None):
                return
            waits = [getattr(reader, '_wait_until', None), *(entry[1] for entry in getattr(reader, '_deferred', {}).values())]
            ends = [end for end in waits if end is not None and end > self.now[0]]
            if ends:
                self.now[0] = min(ends)
        self.fail('the reader did not settle')

    def put(self, text, run_id=RUN, **launch) -> None:
        self.registry.control_put(run_id, record(text, **launch))

    def notes(self, run_id=RUN) -> list:
        return [e['payload']['post_image']['control_record']['note'] for e in self.changed(run_id)]

    def changed(self, run_id=None) -> list:
        return [e for e in self.store.events if e['type'] == 'state.changed'
                and (run_id is None or e['run_id'] == run_id)]

    def unlogged_table(self) -> dict:
        with self.store.transaction() as tx:
            return dict(tx.execute('SELECT run_id, code FROM events_unlogged'))

    # -- the image and its digest

    def test_the_digest_is_spines_rfc8785_digest_of_the_typed_post_image(self) -> None:
        # schemas/control/v1/fixtures/good/event.v1/h30-state-changed-found-by-a-later-scan.json (#368, c77aa2e0).
        row = daemon_module.Run('run-5b1f0c2d9a7e4f36b8c1d2e3f4a5b6c7',
                                'recursion-run-run-5b1f0c2d9a7e4f36b8c1d2e3f4a5b6c7.service', False, 'terminated',
                                '6f1c2d3e-4a5b-4c6d-8e7f-90a1b2c3d4e5', '')
        image = C.post_image(row, None)
        self.assertEqual(image['control_record'], None)
        self.assertEqual(C.image_digest(image), 'c4a0f639e8f212407f0be5bb9b1f0819f5d5cc879513fa77b36b0327e227ba85')

    def test_a_control_text_that_is_not_a_json_object_has_no_image(self) -> None:
        for text in ('not json', '[1, 2]', '{"n": NaN}'):
            with self.subTest(text=text):
                with self.assertRaises(C.BadImage):
                    C.post_image(None, text)

    # -- what is logged

    def test_the_first_pass_ever_logs_snapshots_then_marks_log_commits_and_coalesced_is_null(self) -> None:
        self.registry.add(RUN, 'boot-1')
        self.registry.control_put(RUN2, record('b'))
        self.assertFalse(self.reader.ready)
        self.drain()
        self.assertTrue(self.reader.ready, 'ready once the first pass completed')
        first = self.changed()
        self.assertEqual([(e['run_id'], e['payload']['reason']) for e in first], [(RUN, 'snapshot'), (RUN2, 'snapshot')])
        self.registry.state(RUN, 'running')
        self.drain()
        last = self.changed()[-1]
        self.assertEqual((last['run_id'], last['payload']['reason']), (RUN, 'commit'))
        self.assertEqual(last['payload']['post_image']['daemon_row']['state'], 'running')
        for e in self.changed():
            self.assertIsNone(e['payload']['coalesced'])
            self.assertEqual((e['payload']['entity'], e['payload']['entity_id'], e['source'], e['trust']),
                             ('run', e['run_id'], 'core', 'trusted'))
            self.assertEqual(e['payload']['post_image_sha256'], C.image_digest(e['payload']['post_image']))

    def test_a_b_a_with_a_commit_between_logs_three_events_and_changes_between_reads_coalesce(self) -> None:
        self.drain()
        for text in ('A', 'B', 'A'):
            self.registry.control_put(RUN, record(text))
            self.drain()
        notes = [e['payload']['post_image']['control_record']['note'] for e in self.changed(RUN)]
        self.assertEqual(notes, ['A', 'B', 'A'], 'dedupe is against the last image, not a unique digest')
        for text in ('C', 'D', 'E'):
            self.registry.control_put(RUN, record(text))       # no read between these three
        self.drain()
        self.assertEqual([e['payload']['post_image']['control_record']['note'] for e in self.changed(RUN)][3:], ['E'])
        self.drain()
        self.assertEqual(len(self.changed(RUN)), 4, 'an unchanged run is not logged again')

    def test_a_scan_finds_a_control_only_run_a_daemon_only_run_and_a_record_whose_write_raised(self) -> None:
        self.drain()
        self.registry.mark_changed = None                      # every mark from here on is lost
        self.registry.add(RUN, 'boot-1')                       # daemon only
        self.registry.control_put(RUN2, record('control only'))
        real = daemon_module._sync_directory
        calls = []

        def fail_once(directory):
            if not calls:
                calls.append(directory)
                raise daemon_module.RegistryDurabilityError(5, 'registry directory fsync did not finish')
            return real(directory)
        with mock.patch.object(daemon_module, '_sync_directory', fail_once):
            with self.assertRaises(daemon_module.RegistryDurabilityError):
                self.registry.control_put(RUN3, record('published, then the write raised'))
        self.drain()
        self.assertEqual(self.changed(), [], 'no mark arrived and no scan was due yet')
        self.now[0] += 61                                     # the interval after the last pass
        self.drain()
        found = {e['run_id']: e['payload'] for e in self.changed()}
        self.assertEqual(set(found), {RUN, RUN2, RUN3}, 'each is logged by a later scan, with no restart')
        self.assertIsNone(found[RUN]['post_image']['control_record'])
        self.assertIsNone(found[RUN2]['post_image']['daemon_row'])
        self.assertEqual(found[RUN3]['post_image']['control_record']['note'], 'published, then the write raised')
        self.assertEqual({payload['reason'] for payload in found.values()}, {'reconcile'})

    def test_a_run_logs_under_its_session_and_its_route_decision_once(self) -> None:
        decision = {'engine': 'claude_code', 'why': 'a test value: the reader copies it, never reads it'}
        self.registry.control_put(RUN, record('one', session_id='ses-abc', route_decision=decision, turn_id='turn-1'))
        self.registry.control_put(RUN2, record('two'))
        self.drain()
        self.registry.control_put(RUN, record('three', session_id='ses-abc', route_decision=decision, turn_id='turn-1'))
        self.drain()
        mine = [e for e in self.store.events if e['run_id'] == RUN]
        self.assertEqual([(e['type'], e['session_id'], e['seq']) for e in mine],
                         [('state.changed', 'ses-abc', 1), ('route.decision', 'ses-abc', 2), ('state.changed', 'ses-abc', 3)])
        self.assertEqual((mine[1]['payload'], mine[1]['turn_id']), (decision, 'turn-1'))
        self.assertEqual({e['session_id'] for e in self.store.events if e['run_id'] == RUN2}, {'run:' + RUN2})

    # -- restarts

    def test_a_restart_after_a_publish_the_reader_never_logged_gives_one_reconcile_image(self) -> None:
        self.registry.add(RUN, 'boot-1')
        self.drain()                                           # the first pass ever is done and recorded
        self.registry.mark_changed = None                      # the process dies after this publish, before an append
        self.registry.state(RUN, 'running')
        self.registry.state(RUN, 'terminated', 'exit 0')
        self.marker = H.ChangeMarker()                         # a new process: new marker, new reader, same store
        self.registry.mark_changed = self.marker.mark
        reader = self.new_reader()
        self.drain(reader)
        events = self.changed(RUN)
        self.assertEqual([e['payload']['reason'] for e in events], ['snapshot', 'reconcile'])
        self.assertEqual(events[-1]['payload']['post_image']['daemon_row']['state'], 'terminated',
                         'one reconcile image, and it is the final state')

    def test_a_restart_after_the_readers_commit_logs_no_duplicate(self) -> None:
        self.registry.add(RUN, 'boot-1')
        self.registry.control_put(RUN2, record('x'))
        self.drain()
        before = self.store.events
        self.marker = H.ChangeMarker()
        self.registry.mark_changed = self.marker.mark
        self.drain(self.new_reader())
        self.assertEqual(self.store.events, before, 'the last digest is in the store: nothing is logged twice')

    # -- what a failure leaves

    def test_a_store_that_fails_keeps_the_run_pending_until_a_commit_succeeds(self) -> None:
        self.drain()
        self.store.fail_commit = True
        self.registry.control_put(RUN, record('kept'))
        with self.assertLogs(C.LOG, 'ERROR'):
            self.reader.step()
        self.assertEqual((self.changed(), self.reader._pending), ([], {RUN}))
        self.assertEqual((self.reader._held[0], self.reader.failing), (RUN, True), 'the image read is held')
        self.store.fail_commit = False
        self.drain()
        self.assertEqual([e['run_id'] for e in self.changed()], [RUN])
        self.assertFalse(self.reader.failing)

    def test_an_image_whose_transaction_failed_is_committed_before_a_later_image_of_the_run(self) -> None:
        # S12: the consumer "finishes or retries an image's transaction before it commits a later image of that
        # run". B was read and its transaction failed; C (or A again) is published before the retry.
        self.drain()
        for run_id, later in ((RUN, 'C'), (RUN2, 'A')):
            with self.subTest(later=later):
                self.put('A', run_id)
                self.drain()
                self.put('B', run_id)
                self.store.fail_commit = True
                with self.assertLogs(C.LOG, 'ERROR'):
                    self.assertFalse(self.reader.step())
                self.assertEqual(self.reader._held[0], run_id)
                self.put(later, run_id)
                self.store.fail_commit = False
                self.drain()
                self.assertEqual(self.notes(run_id), ['A', 'B', later], 'the observed B is logged, then the later image')
                self.assertIsNone(self.reader._held)

    def test_a_refused_page_makes_the_loop_wait_and_each_further_refusal_doubles_the_wait(self) -> None:
        class Stopping:                                        # the loop's stop signal: counts turns, records waits
            def __init__(self) -> None:
                self.turns, self.waits = 0, []

            def is_set(self) -> bool:
                self.turns += 1
                return self.turns > 8

            def wait(self, seconds) -> None:
                self.waits.append(seconds)
        stopping = self.reader._stopping = Stopping()
        refused = mock.patch.object(self.registry, 'run_ids', side_effect=OSError('unavailable for now'))
        with refused as reads, self.assertLogs(C.LOG, 'WARNING'):
            self.reader._run()
        self.assertEqual(stopping.waits, [self.marker.poll_interval_s] * 8, 'every turn after the refusal waits')
        self.assertEqual(reads.call_count, 1, 'and no page is read again before the wait has passed')
        waits = []
        with refused, self.assertLogs(C.LOG, 'WARNING'):
            for _ in range(12):
                self.now[0] = self.reader._wait_until          # the clock reaches the end of each wait
                self.assertFalse(self.reader.step())
                waits.append(round(self.reader._wait_until - self.now[0], 6))
        self.assertEqual(waits[:4], [0.2, 0.4, 0.8, 1.6])
        self.assertEqual(waits[-2:], [60.0, 60.0], 'never more than the scan interval')
        self.now[0] = self.reader._wait_until
        self.drain()
        self.assertTrue(self.reader.ready)
        self.assertEqual((self.reader._wait_until, self.reader.failing), (None, False), 'a success ends the wait')

    def test_a_scan_whose_transaction_fails_holds_the_image_and_reads_nothing_until_the_wait_has_passed(self) -> None:
        self.drain()
        self.registry.mark_changed = None
        self.registry.add(RUN, 'boot-1')
        self.registry.add(RUN2, 'boot-1')
        self.now[0] += 61                                      # a scan is due
        self.store.fail_commit = True
        with mock.patch.object(self.registry, 'snapshot', wraps=self.registry.snapshot) as reads:
            with self.assertLogs(C.LOG, 'ERROR'):
                self.assertFalse(self.reader.step(), 'no progress: the loop waits')
            self.assertFalse(self.reader.step())
        self.assertEqual([call.args[0] for call in reads.call_args_list], [RUN], 'one read, then none during the wait')
        self.assertEqual((self.reader._held[0], self.reader._scan_cursor), (RUN, None), 'the cursor stays at the run')
        self.store.fail_commit = False
        self.drain()
        self.assertEqual([e['run_id'] for e in self.changed()], [RUN, RUN2])

    def test_a_failed_scan_state_transaction_waits_like_any_other(self) -> None:
        self.registry.add(RUN, 'boot-1')
        self.store.fail_commit = True                          # the record of the first pass cannot be read
        with mock.patch.object(self.store, 'transaction', wraps=self.store.transaction) as opened:
            with self.assertLogs(C.LOG, 'ERROR'):
                self.assertFalse(self.reader.step())
            self.assertFalse(self.reader.step())
        self.assertEqual((opened.call_count, self.reader.failing), (1, True), 'no second attempt before the wait ends')
        self.store.fail_commit = False
        self.drain()
        self.assertEqual((self.reader.ready, self.reader.failing), (True, False))

    def test_a_completed_pass_that_cannot_be_recorded_waits_and_completes_later(self) -> None:
        self.registry.add(RUN, 'boot-1')
        reader = self.new_reader(page=1)
        self.assertTrue(reader.step())                         # the first page: RUN
        self.store.fail_commit = True
        with self.assertLogs(C.LOG, 'ERROR'):
            self.assertFalse(reader.step())                    # the empty page: recording the pass fails
        self.assertEqual((reader.ready, reader.failing), (False, True))
        self.store.fail_commit = False
        self.drain(reader)
        self.assertEqual((reader.ready, reader.failing), (True, False))

    def test_a_run_the_registry_will_not_read_holds_up_neither_the_scan_nor_other_runs(self) -> None:
        self.registry.mark_changed = None
        for run_id in (RUN, RUN2, RUN3):
            self.registry.add(run_id, 'boot-1')
        self.registry.mark_changed = self.marker.mark
        real = self.registry.snapshot
        reads = []

        def refuse_one(run_id):
            reads.append(run_id)
            if run_id == RUN2:
                raise daemon_module.RegistryIntegrityError('this run cannot be read for now')
            return real(run_id)
        with mock.patch.object(self.registry, 'snapshot', refuse_one), self.assertLogs(C.LOG, 'WARNING'):
            for _ in range(10):                                # a pass of three runs needs two steps
                if not self.reader.step():
                    break
            self.assertTrue(self.reader.ready, 'the pass completed past the run it could not read')
            self.assertEqual(({e['run_id'] for e in self.changed()}, self.reader._pending), ({RUN, RUN3}, {RUN2}))
            self.assertTrue(self.reader.failing)
            self.put('marked behind it', RUN3)                 # sorts after the refused run
            reads.clear()
            self.reader.step()
            self.assertEqual(reads, [RUN3], 'the refused run waits for its own time; the run behind it is read')
            self.assertEqual(self.changed(RUN3)[-1]['payload']['post_image']['control_record']['note'], 'marked behind it')
        self.drain()
        self.assertEqual([e['payload']['reason'] for e in self.changed(RUN2)], ['snapshot'], "it keeps the scan's reason")
        self.assertFalse(self.reader.failing)

    def test_a_run_with_no_typed_post_image_is_listed_read_at_every_scan_and_logged_once_it_has_one(self) -> None:
        def nested(depth):
            value = 1
            for _ in range(depth):
                value = {'a': value}
            return value
        texts = {'run-bigint': '{"n": 123456789012345678901234567890}', 'run-inf': '{"n": 1e400}',
                 'run-surrogate': '{"s": "\\ud800"}', 'run-nan': '{"n": NaN}', 'run-notjson': 'not json',
                 'run-list': '[1, 2]', 'run-deep': json.dumps(nested(62)),  # RFC 8785 text, but too deep for the store
                 'run-ok': record('ok')}
        for run_id, text in texts.items():
            self.registry.control_put(run_id, text)
        with self.assertLogs(C.LOG, 'ERROR'):
            self.drain()
        self.assertTrue(self.reader.ready)
        self.assertEqual({e['run_id'] for e in self.changed()}, {'run-ok'})
        self.assertEqual(self.reader.unlogged, {
            'run-bigint': 'no_canonical_text:integer', 'run-inf': 'no_canonical_text:number',
            'run-surrogate': 'no_canonical_text:surrogate', 'run-nan': 'control_text_not_json:NaN',
            'run-notjson': 'control_text_not_json', 'run-list': 'control_text_not_an_object',
            'run-deep': 'store_refused:value_too_deep'})
        self.assertEqual(self.unlogged_table(), self.reader.unlogged, "the lane's table holds the same")
        self.assertEqual((self.reader._pending, self.reader.failing), (set(), False), 'none is spun on')
        self.now[0] += 61
        with self.assertLogs(C.LOG, 'ERROR') as again:
            self.drain()
        self.assertEqual(len(again.records), 7, 'the next scan read each of them again')
        self.put('typed now', 'run-bigint')
        self.drain()
        self.assertEqual(self.notes('run-bigint'), ['typed now'])
        self.assertNotIn('run-bigint', self.reader.unlogged)

    def test_a_registry_that_refuses_a_read_keeps_the_run_pending(self) -> None:
        self.drain()
        self.registry.control_put(RUN, record('kept'))
        with mock.patch.object(self.registry, 'snapshot', side_effect=daemon_module.RegistryIntegrityError('closed')):
            with self.assertLogs(C.LOG, 'WARNING'):
                self.reader.step()
        self.assertEqual((self.changed(), self.reader._pending), ([], {RUN}))
        self.drain()
        self.assertEqual([e['run_id'] for e in self.changed()], [RUN])

    def test_the_reader_never_pages_the_registry_inside_a_transaction(self) -> None:
        self.drain()
        self.now[0] += 61                                      # a scan is due
        with mock.patch.object(self.registry, 'run_ids', wraps=self.registry.run_ids) as pages:
            with self.store.transaction():
                with self.assertRaises(D.LockOrderError) as caught:
                    self.reader.step()
        self.assertEqual((str(caught.exception), pages.call_count), ('lock_order:daemon.registry', 0))

    def test_the_reader_never_reads_the_registry_inside_a_transaction(self) -> None:
        with self.store.transaction():
            with self.assertRaises(D.LockOrderError) as caught:
                self.reader._log_run(RUN, 'commit')
        self.assertEqual(str(caught.exception), 'lock_order:daemon.registry')

    # -- scans

    def test_a_scan_pages_and_marks_are_worked_in_between(self) -> None:
        self.registry.mark_changed = None
        for run_id in ('run-1', 'run-2', 'run-3', 'run-4', 'run-5'):
            self.registry.add(run_id, 'boot-1')
        self.registry.mark_changed = self.marker.mark
        reader = self.new_reader(page=2)
        pages = []
        real = self.registry.run_ids

        def recorded(after, limit):
            got = real(after, limit)
            pages.append((after, got))
            return got
        with mock.patch.object(self.registry, 'run_ids', recorded):
            self.assertTrue(reader.step())
            self.registry.control_put('run-9', record('marked during the scan'))
            self.assertTrue(reader.step())
            self.assertIn('run-9', {e['run_id'] for e in self.changed()}, 'a mark is worked between two pages')
            self.drain(reader)
        # run-9 was made during the pass and sorts after the cursor, so this pass reads it too (third page).
        self.assertEqual(pages, [(None, ['run-1', 'run-2']), ('run-2', ['run-3', 'run-4']),
                                 ('run-4', ['run-5', 'run-9']), ('run-9', [])],
                         'each page starts after the last id of the one before; an empty page ends the pass')
        self.assertEqual({e['run_id'] for e in self.changed()}, {'run-1', 'run-2', 'run-3', 'run-4', 'run-5', 'run-9'})

    def test_a_drop_asks_for_a_scan_and_does_not_restart_one_in_progress(self) -> None:
        self.registry.mark_changed = None
        for run_id in ('run-1', 'run-2', 'run-3'):
            self.registry.add(run_id, 'boot-1')
        reader = self.new_reader(page=1)
        self.assertTrue(reader.step())
        self.assertEqual(reader._scan_cursor, 'run-1')
        self.marker._dropped = True
        self.assertTrue(reader.step())
        self.assertEqual(reader._scan_cursor, 'run-2', 'the scan in progress goes on from where it was')
        self.drain(reader)
        self.assertTrue(reader.ready)
        self.marker._dropped = True                            # no scan in progress now: a drop starts one
        self.assertTrue(reader.step())
        self.assertEqual(reader._scan_cursor, 'run-1')

    # -- Stop and the reader

    def test_a_consumer_paused_inside_take_while_a_stop_is_issued_drops_the_mark_and_a_later_scan_logs_it(self) -> None:
        self.registry.add(RUN, 'boot-1')
        self.drain()
        self.marker._lock.acquire()                            # the consumer is inside take(), holding the marker
        try:
            done = threading.Event()
            stopper = threading.Thread(target=lambda: (self.registry.request_stop(RUN), done.set()))
            stopper.start()
            self.assertTrue(done.wait(BOUND_S), 'the Stop completed while the consumer held the marker')
            stopper.join(BOUND_S)
        finally:
            self.marker._lock.release()
        self.assertEqual((self.marker._ids, self.marker._dropped), (set(), True),
                         'the producer dropped its mark instead of waiting')
        self.drain()                                           # the drop asks for a scan, which logs the Stop
        last = self.changed(RUN)[-1]
        self.assertEqual((last['payload']['reason'], last['payload']['post_image']['daemon_row']['stop']), ('reconcile', True))

    def test_a_reader_holding_the_registrys_lock_makes_a_stop_wait_and_the_stop_then_answers_truly(self) -> None:
        self.registry.add(RUN, 'boot-1')
        self.drain()
        self.registry.state(RUN, 'running')
        inside, release, stopped = threading.Event(), threading.Event(), threading.Event()
        real = self.registry._has_control_table

        def paused():
            if threading.current_thread().name == 'reader':
                inside.set()
                release.wait(BOUND_S)
            return real()
        with mock.patch.object(self.registry, '_has_control_table', paused):
            reader = threading.Thread(target=self.reader.step, name='reader')
            reader.start()
            self.assertTrue(inside.wait(BOUND_S), 'the reader is inside snapshot, holding the registry lock')
            stopper = threading.Thread(target=lambda: (self.registry.request_stop(RUN), stopped.set()))
            stopper.start()
            self.assertFalse(stopped.wait(NOT_YET_S), 'the Stop waits for the lock the reader holds')
            release.set()
            reader.join(BOUND_S)
            self.assertTrue(stopped.wait(BOUND_S))
            stopper.join(BOUND_S)
        row = self.registry.get(RUN)
        self.assertEqual((row.stop_requested, row.state), (True, 'stopping'), 'the Stop recorded what it does on the base')
        self.drain()
        self.assertEqual(self.changed(RUN)[-1]['payload']['post_image']['daemon_row']['state'], 'stopping')

    def test_a_marker_that_raises_has_no_effect_on_the_writer(self) -> None:
        self.registry.mark_changed = mock.Mock(side_effect=RuntimeError('a broken consumer'))
        self.registry.add(RUN, 'boot-1')                       # returns normally
        self.assertEqual(self.registry.get(RUN).state, 'starting')

    # -- the service

    def test_the_service_starts_a_thread_that_logs_and_stops_by_joining_it(self) -> None:
        seen = threading.Event()
        self.store.subscribe(lambda events: any(e['type'] == 'state.changed' for e in events) and seen.set())
        context = mock.Mock(spec=['registry', 'store'])
        context.registry, context.store = self.registry, self.store
        self.marker.poll_interval_s = 0.01
        service = C.ChangeReader(self.marker)
        service.start(context)
        try:
            self.registry.add(RUN, 'boot-1')
            self.assertTrue(seen.wait(BOUND_S), 'the service thread logged the change')
            # The first pass may have found the run on its first page; it completes at the empty page after it.
            deadline = time.monotonic() + BOUND_S
            while not service.ready and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(service.ready, 'the first pass of this start completed')
        finally:
            service.stop(context)
        self.assertIsNone(service._thread, 'stop returned after the thread joined')
        self.assertTrue(service.ready)

    def test_register_brings_the_store_the_marker_and_the_service(self) -> None:
        hooks = H.Hooks()
        reader = C.register(hooks)
        hooks.freeze()
        self.assertIs(hooks._single['event_store'], C.open_store)
        self.assertIs(reader._marker, hooks.changes)
        self.assertEqual([service.name for service in hooks._services], ['events'])
        store = C.open_store(self.folder / 'state')
        self.addCleanup(store.close)
        self.assertEqual(os.path.basename(store.path), 'events.sqlite3')
        with store.transaction() as tx:
            self.assertEqual(tx.execute('SELECT count(*) FROM events_run_images'), [(0,)])

    # -- reasons, logs and the route decision

    def test_a_run_already_logged_is_reconcile_when_a_first_pass_was_cut_short(self) -> None:
        self.registry.mark_changed = None
        self.registry.add(RUN, 'boot-1')
        self.registry.add(RUN2, 'boot-1')
        reader = self.new_reader(page=1)
        self.assertTrue(reader.step())                         # the first page of the first pass ever: RUN alone
        self.assertFalse(reader.ready)
        self.registry.state(RUN, 'running')                    # then the process dies: a new start, the same store
        self.marker = H.ChangeMarker()
        self.registry.mark_changed = self.marker.mark
        self.drain(self.new_reader())
        self.assertEqual([e['payload']['reason'] for e in self.changed(RUN)], ['snapshot', 'reconcile'])
        self.assertEqual([e['payload']['reason'] for e in self.changed(RUN2)], ['snapshot'],
                         'a run the cut-short pass never reached is still its first record')

    def test_a_record_that_names_a_session_later_moves_the_run_to_that_session(self) -> None:
        self.registry.add(RUN, 'boot-1')
        self.drain()
        self.put('joined', session_id='ses-abc')
        self.drain()
        self.put('names no session')                           # the run's log stays where it went
        self.drain()
        self.assertEqual([e['session_id'] for e in self.changed(RUN)], ['run:' + RUN, 'ses-abc', 'ses-abc'])
        self.assertEqual([e['seq'] for e in self.changed(RUN)], [1, 1, 2], 'each log numbers its own events')
        self.assertEqual(self.store.rows[('events_run_images', RUN)]['log_id'], 'ses-abc')

    def test_a_route_decision_that_is_not_an_object_is_not_logged(self) -> None:
        self.put('one', route_decision='claude_code')
        self.drain()
        self.assertEqual([e['type'] for e in self.store.events], ['state.changed'])
        self.put('two', route_decision={'engine': 'claude_code'})
        self.drain()
        self.assertEqual([e['type'] for e in self.store.events], ['state.changed', 'state.changed', 'route.decision'])

    # -- the service's start, stop and options

    def context(self):
        context = mock.Mock(spec=['registry', 'store'])
        context.registry, context.store = self.registry, self.store
        return context

    def test_stop_is_safe_after_a_start_whose_thread_never_started(self) -> None:
        context, service = self.context(), C.ChangeReader(self.marker)
        with mock.patch.object(threading.Thread, 'start', side_effect=RuntimeError("can't start new thread")):
            with self.assertRaisesRegex(RuntimeError, "can't start new thread"):
                service.start(context)
        service.stop(context)
        service.stop(context)
        self.assertIsNone(service._thread)
        service.start(context)                                 # and it can be started after all
        service.stop(context)
        self.assertIsNone(service._thread)

    def test_a_second_start_is_refused_and_every_start_scans_again(self) -> None:
        context, service = self.context(), C.ChangeReader(self.marker)
        self.marker.poll_interval_s = 0.01
        service.start(context)
        try:
            with self.assertRaisesRegex(RuntimeError, 'already started'):
                service.start(context)
        finally:
            service.stop(context)
        self.assertTrue(service.ready)
        self.registry.mark_changed = None                      # a run made while the service is stopped, its mark lost
        self.registry.add(RUN, 'boot-1')
        seen, observed, real_step = threading.Event(), [], service.step
        self.store.subscribe(lambda events: seen.set())

        def step():
            if not observed:
                observed.append(service.ready)
            return real_step()
        service.step = step
        service.start(context)
        try:
            self.assertTrue(seen.wait(BOUND_S), 'the new start scanned and logged the run')
        finally:
            service.stop(context)
        self.assertEqual(observed, [False], 'ready belongs to a start: false until its own first pass')
        self.assertEqual([e['payload']['reason'] for e in self.changed(RUN)], ['reconcile'])

    def test_stop_returns_only_after_its_worker_has_left_a_source_read(self) -> None:
        entered, resume, stopped = threading.Event(), threading.Event(), threading.Event()
        real = self.registry.snapshot

        def paused(run_id):
            if threading.current_thread().name == 'events-change-reader':
                entered.set()
                resume.wait(BOUND_S)
            return real(run_id)
        self.registry.add(RUN, 'boot-1')
        context, service = self.context(), C.ChangeReader(self.marker)
        with mock.patch.object(self.registry, 'snapshot', paused):
            service.start(context)
            self.assertTrue(entered.wait(BOUND_S))
            worker = service._thread
            closer = threading.Thread(target=lambda: (service.stop(context), stopped.set()))
            closer.start()
            try:
                self.assertFalse(stopped.wait(NOT_YET_S), 'stop has not returned while its worker is inside the read')
            finally:
                resume.set()
                closer.join(BOUND_S)
        self.assertTrue(stopped.is_set())
        self.assertFalse(worker.is_alive())

    def test_one_step_works_at_most_a_slice_of_marked_runs(self) -> None:
        reader = self.new_reader(slice_size=2)
        self.drain(reader)
        for n in range(5):
            self.registry.add(f'run-s{n}', 'boot-1')
        logged = []
        for _ in range(3):
            reader.step()
            logged.append(len(self.changed()))
        self.assertEqual(logged, [2, 4, 5])

    def test_the_options_are_checked(self) -> None:
        cases = (('page', (0, -1, 1.5, True, '2')), ('slice_size', (0, -1, 2.0, False)),
                 ('scan_interval_s', (0, -1, float('nan'), float('inf'), True, '60')))
        for field, values in cases:
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError) as caught:
                        C.ChangeReader(self.marker, **{field: value})
                    self.assertEqual(str(caught.exception), field)
        with self.assertRaises(TypeError):
            C.ChangeReader(None)
        with self.assertRaisesRegex(RuntimeError, 'not bound'):
            C.ChangeReader(self.marker).step()

    def test_the_image_functions_refuse_what_is_not_a_run(self) -> None:
        for row, text, code in (({'run_id': RUN}, None, 'daemon_row_not_a_run'), (None, b'{}', 'control_text_not_text')):
            with self.subTest(code=code):
                with self.assertRaises(C.BadImage) as caught:
                    C.post_image(row, text)
                self.assertEqual(str(caught.exception), code)
        for image in (None, {}, {'daemon_row': None}, {'daemon_row': None, 'control_record': None, 'more': 1}):
            with self.subTest(image=image):
                with self.assertRaises(C.BadImage):
                    C.image_digest(image)

    def test_a_consumer_paused_inside_the_real_take_while_the_daemon_stops_the_run(self) -> None:
        """The real ``take`` holds the marker's lock on the consumer's thread while ``Daemon.stop`` runs: the Stop
        completes with the base's answer, the producer drops its mark, and a later scan logs the Stop."""
        class Manager:                                         # the user manager, faked: a unit runs until stopped
            def __init__(self) -> None:
                self.units, self.stops = {}, []

            def start(self, unit, argv) -> None:
                self.units[unit] = 'running'

            def stop(self, unit) -> None:
                self.stops.append(unit)
                self.units.pop(unit, None)

            def observe(self, unit):
                return self.units.get(unit, 'gone')

            def processes_gone(self, unit) -> bool:
                return unit not in self.units

            def owned_units(self) -> set:
                return set(self.units)
        manager = Manager()
        daemon = daemon_module.Daemon(self.registry, manager, boot_id=lambda: 'boot-1')
        daemon.start(RUN, [])
        self.drain()
        inside, release, real_lock = threading.Event(), threading.Event(), self.marker._lock

        class PausedLock:                                      # the marker's lock; take pauses once it holds it
            def acquire(self, *args, **kwargs):
                return real_lock.acquire(*args, **kwargs)

            def release(self) -> None:
                real_lock.release()

            def __enter__(self):
                real_lock.acquire()
                if threading.current_thread().name == 'consumer':
                    inside.set()
                    release.wait(BOUND_S)
                return self

            def __exit__(self, *exc) -> None:
                real_lock.release()
        self.marker._lock = PausedLock()
        consumer = threading.Thread(target=self.reader.step, name='consumer')
        answer = []
        consumer.start()
        try:
            self.assertTrue(inside.wait(BOUND_S), 'the consumer is inside take, holding the marker')
            stopper = threading.Thread(target=lambda: answer.append(daemon.stop(RUN)))
            stopper.start()
            stopper.join(BOUND_S)
            self.assertEqual(len(answer), 1, 'the Stop completed while the consumer was inside take')
            self.assertTrue(self.marker._dropped, 'the producer dropped its mark instead of waiting')
        finally:
            release.set()
            consumer.join(BOUND_S)
            self.marker._lock = real_lock
        self.assertEqual((answer[0].stop_requested, answer[0].state, len(manager.stops)), (True, 'terminated', 1))
        self.now[0] += 61                                      # the drop can race with take; the periodic scan cannot
        self.drain()
        self.assertEqual(self.changed(RUN)[-1]['payload']['post_image']['daemon_row']['state'], 'terminated')

    # -- round 2 of #403's reads

    def test_a_start_interrupted_after_its_thread_launched_keeps_it_owned_and_stop_joins_it(self) -> None:
        # Sol r2, required 1: an interruption inside Thread.start's own wait, after the thread was launched.
        self.registry.add(RUN, 'boot-1')
        inside, resume, done = threading.Event(), threading.Event(), threading.Event()
        real_thread, real_read, workers = threading.Thread, self.registry.snapshot, []

        def make_thread(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            if thread.name == 'events-change-reader':
                workers.append(thread)
                started_wait = thread._started.wait

                def wait_then_interrupt(*a, **kw):
                    started_wait(*a, **kw)
                    raise KeyboardInterrupt('start interrupted after the launch')
                thread._started.wait = wait_then_interrupt
            return thread

        def paused(run_id):
            if threading.current_thread().name == 'events-change-reader':
                inside.set()
                resume.wait(BOUND_S)
            return real_read(run_id)
        service, context = C.ChangeReader(self.marker), self.context()
        closer = threading.Thread(target=lambda: (service.stop(context), done.set()))
        with mock.patch.object(self.registry, 'snapshot', paused), mock.patch.object(threading, 'Thread', make_thread):
            try:
                with self.assertRaisesRegex(KeyboardInterrupt, 'after the launch'):
                    service.start(context)
                self.assertTrue(inside.wait(BOUND_S), 'the launched thread is inside a source read')
                closer.start()
                self.assertFalse(done.wait(NOT_YET_S), 'stop has not returned while that thread is inside the read')
            finally:
                resume.set()
                if closer.ident is not None:
                    closer.join(BOUND_S)
                else:
                    service.stop(context)
        self.assertTrue(done.is_set())
        self.assertFalse(workers[0].is_alive(), 'stop returned after joining it')
        self.assertIsNone(service._thread)

    def test_a_thread_whose_start_raised_before_it_ran_does_nothing_when_it_runs_late(self) -> None:
        # Each start has its own stop signal: a launch that runs only after stop and a new start reads its own.
        made, steps = [], []

        class Launch:                                          # a thread that runs only when the test runs it
            def __init__(self, target, args, name, daemon) -> None:
                self.target, self.args = target, args
                made.append(self)

            def start(self) -> None:
                if len(made) == 1:
                    raise KeyboardInterrupt('start interrupted before the thread ran')

            def is_alive(self) -> bool:
                return False
        service, context = C.ChangeReader(self.marker), self.context()

        def step():
            steps.append(1)
            service._stopping.set()                            # ends a loop that reads the current start's signal
            return False
        with mock.patch.object(threading, 'Thread', Launch):
            with self.assertRaises(KeyboardInterrupt):
                service.start(context)
            service.stop(context)
            self.assertIsNone(service._thread, 'a thread that never began is not joined')
            service.start(context)                             # a new start, with a signal of its own, not set
            with mock.patch.object(service, 'step', step):
                made[0].target(*made[0].args)                  # the first launch runs late: it reads its own signal
        self.assertEqual(steps, [], 'the late thread did no step')
        service.stop(context)

    def test_a_scan_does_not_read_a_deferred_run_before_its_own_wait_has_passed(self) -> None:
        # Sol r2, required 2 (its first probe): dropped marks ask for scans; the clock does not move.
        self.registry.add(RUN, 'boot-1')
        self.registry.add(RUN2, 'boot-1')
        real, refused = self.registry.snapshot, []

        def refuse_one(run_id):
            if run_id == RUN:
                refused.append(self.now[0])
                raise OSError('unreadable row')
            return real(run_id)
        with mock.patch.object(self.registry, 'snapshot', refuse_one), self.assertLogs(C.LOG, 'WARNING'):
            self.reader.step()
            self.reader.step()
            self.assertTrue(self.reader.ready)
            for _ in range(4):
                self.marker._dropped = True
                self.assertTrue(self.reader.step(), 'a scan pages past the deferred run')
                self.reader.step()
        self.assertEqual(refused, [0.0], 'read once, then not before its own wait had passed')

    def test_a_deferred_run_keeps_the_reason_it_was_first_read_for_through_later_scans(self) -> None:
        # Sol r2, required 2 (its second probe).
        self.registry.add(RUN, 'boot-1')
        with mock.patch.object(self.registry, 'snapshot', side_effect=OSError('unreadable row')):
            with self.assertLogs(C.LOG, 'WARNING'):
                self.reader.step()
                self.reader.step()
                self.marker._dropped = True
                self.reader.step()
                self.reader.step()
        self.now[0] = self.reader._deferred[RUN][1]
        self.drain()
        self.assertEqual([e['payload']['reason'] for e in self.changed(RUN)], ['snapshot'])

    def test_failed_records_of_a_completed_pass_double_the_wait(self) -> None:
        # Sol r2, required 3: the page read between two failed records of the pass does not end the backoff.
        self.registry.add(RUN, 'boot-1')
        reader = self.new_reader(page=1)
        self.assertTrue(reader.step())
        self.store.fail_commit = True
        waits = []
        with self.assertLogs(C.LOG, 'ERROR'):
            for _ in range(4):
                self.assertFalse(reader.step())
                waits.append(round(reader._wait_until - self.now[0], 6))
                self.now[0] = reader._wait_until
        self.assertEqual(waits, [0.1, 0.2, 0.4, 0.8])
        self.store.fail_commit = False
        self.drain(reader)
        self.assertEqual((reader.ready, reader.failing, reader._backoff), (True, False, 0.0))

    def unloggable_after_a_pass(self, text='not json') -> None:
        self.drain()
        self.registry.mark_changed = None                      # only a scan finds it
        self.registry.control_put(RUN, text)
        self.now[0] += 61

    def test_failed_listings_of_an_unlogged_run_double_the_wait_and_keep_the_scan_at_it(self) -> None:
        self.unloggable_after_a_pass()
        self.store.fail_commit = True
        waits = []
        with self.assertLogs(C.LOG, 'ERROR'):
            for _ in range(4):
                self.assertFalse(self.reader.step())
                waits.append(round(self.reader._wait_until - self.now[0], 6))
                self.now[0] = self.reader._wait_until
        self.assertEqual(waits, [0.1, 0.2, 0.4, 0.8])
        self.assertEqual((self.reader._scan_cursor, self.reader.unlogged), (None, {}), 'the scan stays at the run')
        self.store.fail_commit = False
        with self.assertLogs(C.LOG, 'ERROR'):
            self.drain()
        self.assertEqual(self.unlogged_table(), {RUN: 'control_text_not_json'})
        self.assertEqual((self.reader.failing, self.reader._backoff), (False, 0.0), 'the written listing ends the backoff')

    def test_a_refusal_of_the_other_source_starts_its_own_wait(self) -> None:
        self.unloggable_after_a_pass()
        self.store.fail_commit = True
        with self.assertLogs(C.LOG, 'ERROR'):
            self.reader.step()
            self.now[0] = self.reader._wait_until
            self.reader.step()                                 # the store refused twice: 0.1, then 0.2
        self.assertEqual(round(self.reader._wait_until - self.now[0], 6), 0.2)
        self.now[0] = self.reader._wait_until
        with mock.patch.object(self.registry, 'run_ids', side_effect=OSError('unavailable for now')):
            with self.assertLogs(C.LOG, 'WARNING'):
                self.reader.step()
        self.assertEqual(round(self.reader._wait_until - self.now[0], 6), 0.1, 'a page refused: its own first wait')

    def test_a_page_read_after_refused_pages_ends_their_backoff(self) -> None:
        refused = mock.patch.object(self.registry, 'run_ids', side_effect=OSError('unavailable for now'))
        with refused, self.assertLogs(C.LOG, 'WARNING'):
            for _ in range(6):
                self.reader.step()
                self.now[0] = self.reader._wait_until
        self.assertEqual(round(self.reader._backoff, 6), 3.2)
        self.reader.step()                                     # the page is read: an empty registry, a pass completed
        self.assertTrue(self.reader.ready)
        self.marker._dropped = True
        with refused, self.assertLogs(C.LOG, 'WARNING'):
            self.reader.step()
        self.assertEqual(round(self.reader._wait_until - self.now[0], 6), 0.1, 'a new refusal waits the first wait')

    def test_the_poll_interval_is_read_as_a_number_from_the_floor_to_the_scan_interval(self) -> None:
        cases = ((0.05, 0.05), (0, C.MIN_POLL_S), (-5, C.MIN_POLL_S), (float('nan'), C.MIN_POLL_S),
                 (True, C.MIN_POLL_S), ('0.1', C.MIN_POLL_S), (float('inf'), 60.0), (1e9, 60.0))
        for value, read in cases:
            with self.subTest(value=value):
                self.marker.poll_interval_s = value
                self.assertEqual(self.reader._poll_s(), read)

        class Bare:                                            # a marker with take and no poll interval
            def take(self):
                return [], False
        self.assertEqual(C.ChangeReader(Bare())._poll_s(), C.MIN_POLL_S)

    def test_a_marker_whose_poll_interval_is_0_or_less_still_makes_a_refusal_wait(self) -> None:
        # DeepSeek r2, M1: at 0 the reader paged 42,244 times a second; below 0 a step raised OverflowError.
        def refuse(after, limit):                              # a new exception each time: one raised again grows
            raise OSError('unavailable')                       # its traceback at every raise
        for value in (0, -5):
            with self.subTest(value=value):
                reader = self.new_reader()
                self.marker.poll_interval_s = value
                with mock.patch.object(self.registry, 'run_ids', side_effect=refuse) as reads:
                    with self.assertLogs(C.LOG, 'WARNING'):
                        for _ in range(1100):
                            if reader._wait_until is not None:
                                self.now[0] = reader._wait_until
                            self.assertFalse(reader.step())
                    self.assertEqual(reads.call_count, 1100)
                    self.assertEqual(reader._backoff, 60.0)
                    self.assertGreater(reader._wait_until, self.now[0])
                    self.assertFalse(reader.step(), 'no read before the wait has passed')
                    self.assertEqual(reads.call_count, 1100)

    def test_the_loop_sleeps_the_poll_interval_as_the_reader_reads_it(self) -> None:
        class Stopping:                                        # the loop's stop signal: counts turns, records waits
            def __init__(self) -> None:
                self.turns, self.waits = 0, []

            def is_set(self) -> bool:
                self.turns += 1
                return self.turns > 3

            def wait(self, seconds) -> None:
                self.waits.append(seconds)
        stopping = self.reader._stopping = Stopping()
        self.marker.poll_interval_s = -5
        with mock.patch.object(self.registry, 'run_ids', side_effect=OSError('unavailable')), self.assertLogs(C.LOG, 'WARNING'):
            self.reader._run()
        self.assertEqual(stopping.waits, [C.MIN_POLL_S] * 3)

    def test_a_refused_runs_first_wait_is_the_poll_interval_as_the_reader_reads_it(self) -> None:
        self.marker.poll_interval_s = 0
        self.registry.add(RUN, 'boot-1')
        with mock.patch.object(self.registry, 'snapshot', side_effect=OSError('unreadable row')):
            with self.assertLogs(C.LOG, 'WARNING'):
                self.reader.step()
        self.assertEqual(round(self.reader._deferred[RUN][1] - self.now[0], 6), C.MIN_POLL_S)

    def test_a_deferred_run_takes_no_place_in_a_slice(self) -> None:
        reader = self.new_reader(slice_size=1)
        self.drain(reader)
        self.registry.add(RUN, 'boot-1')
        self.registry.add(RUN2, 'boot-1')
        real, reads = self.registry.snapshot, []

        def refuse_one(run_id):
            reads.append(run_id)
            if run_id == RUN:
                raise OSError('unreadable row')
            return real(run_id)
        with mock.patch.object(self.registry, 'snapshot', refuse_one), self.assertLogs(C.LOG, 'WARNING'):
            reader.step()
            reads.clear()
            reader.step()
        self.assertEqual(reads, [RUN2], 'the slice of one is the run that is due')

    def test_a_failed_listing_ends_the_step(self) -> None:
        self.drain()
        self.registry.control_put(RUN, 'not json')
        self.put('B', RUN2)
        self.store.fail_commit = True
        with mock.patch.object(self.registry, 'snapshot', wraps=self.registry.snapshot) as reads:
            with self.assertLogs(C.LOG, 'ERROR'):
                self.assertFalse(self.reader.step())
        self.assertEqual([call.args[0] for call in reads.call_args_list], [RUN], 'nothing more after the failed listing')
        self.assertEqual(self.reader._pending, {RUN, RUN2})
        self.store.fail_commit = False
        self.now[0] = self.reader._wait_until
        with self.assertLogs(C.LOG, 'ERROR'):
            self.drain()
        self.assertEqual((self.unlogged_table(), self.notes(RUN2)), ({RUN: 'control_text_not_json'}, ['B']))

    def test_an_unlogged_run_is_in_the_lanes_table_and_a_new_start_reads_it_back(self) -> None:
        # DeepSeek r2, M2: unlogged was in memory only.
        self.put('typed', RUN)
        self.registry.control_put(RUN2, 'not json')
        with self.assertLogs(C.LOG, 'ERROR'):
            self.drain()
        self.assertEqual((self.unlogged_table(), self.reader.ready), ({RUN2: 'control_text_not_json'}, True))
        restarted = self.new_reader(page=1)
        self.assertEqual(restarted.unlogged, {})
        self.assertTrue(restarted.step())                      # the scan state, then a page of one run: RUN alone
        self.assertEqual(restarted._scan_cursor, RUN)
        self.assertEqual(restarted.unlogged, {RUN2: 'control_text_not_json'}, 'read back before the scan reaches it')
        self.registry.control_put(RUN2, '[1, 2]')
        with self.assertLogs(C.LOG, 'ERROR'):
            self.drain(restarted)
        self.assertEqual(self.unlogged_table(), {RUN2: 'control_text_not_an_object'}, 'a new code replaces the old')
        self.put('typed now', RUN2)
        self.drain(restarted)
        self.assertEqual((self.unlogged_table(), restarted.unlogged, self.notes(RUN2)), ({}, {}, ['typed now']))

    def test_an_unlogged_run_whose_code_is_unchanged_is_not_written_again(self) -> None:
        self.registry.control_put(RUN, 'not json')
        with self.assertLogs(C.LOG, 'ERROR'):
            self.drain()
        self.now[0] += 61
        with mock.patch.object(self.store, 'transaction', wraps=self.store.transaction) as opened:
            with self.assertLogs(C.LOG, 'ERROR'):
                self.drain()
        self.assertEqual(opened.call_count, 0, 'the scan read it again and wrote nothing')

    def test_a_held_image_the_store_then_refuses_whose_listing_fails_ends_the_step(self) -> None:
        self.drain()
        self.put('A')
        self.put('B', RUN2)
        real, calls = self.reader._commit, []

        def commit(run_id, image, digest, reason):
            calls.append(run_id)
            if len(calls) == 1:
                raise OSError('the commit failed')
            if len(calls) == 2:
                self.store.fail_commit = True                  # and the listing that follows fails too
                raise ValueError('event_refused')
            return real(run_id, image, digest, reason)
        with mock.patch.object(self.reader, '_commit', commit), self.assertLogs(C.LOG, 'ERROR'):
            self.assertFalse(self.reader.step())
            self.assertEqual(self.reader._held[0], RUN)
            self.now[0] = self.reader._wait_until
            with mock.patch.object(self.registry, 'snapshot', wraps=self.registry.snapshot) as reads:
                self.assertFalse(self.reader.step())
            self.assertEqual(reads.call_count, 0, 'nothing more after the failed listing')
        self.store.fail_commit = False
        self.now[0] = self.reader._wait_until
        self.drain()
        self.assertEqual((self.notes(), self.notes(RUN2)), (['A'], ['B']))

    def test_the_codes_have_one_shape(self) -> None:
        # DeepSeek r2, L2.
        self.assertEqual(C._code(C.BadImage('control_text_not_json:NaN'), 'store_refused'), 'control_text_not_json:NaN')
        self.assertEqual(C._code(ValueError('source_not_allowed_for:state.changed'), 'store_refused'),
                         'store_refused:source_not_allowed_for:state.changed')
        self.assertEqual(C._code(ValueError('a sentence, not a code'), 'store_refused'), 'store_refused')
        self.assertEqual(C._code(ValueError('x' * 81), 'no_canonical_text'), 'no_canonical_text')

    def test_the_lanes_tables_change_only_with_their_version(self) -> None:
        # DeepSeek r2, M3: the text changed at version 1. A new text needs a new version (and a new digest here).
        self.assertEqual((C.LANE_VERSION, hashlib.sha256(C.LANE_DDL.encode('utf-8')).hexdigest()),
                         (2, 'ecd17983f503d37f5e9e4e4b4a704cf2d07d0249b73460b0314abfbaaf9a9691'))

    def test_a_store_declared_by_another_version_is_refused_with_the_version_it_holds(self) -> None:
        state = self.folder / 'old'
        state.mkdir()
        old = D.EventStore(str(state / C.STORE_FILE))
        old.register_tables(C.LANE, 1, C.LANE_DDL.rsplit('; CREATE TABLE events_unlogged', 1)[0])
        old.close()
        with self.assertRaises(D.StoreError) as caught:
            C.open_store(state)
        self.assertTrue(str(caught.exception).startswith('declaration_changed: '), str(caught.exception))
        self.assertIn('tables of version 1; this code declares version 2', str(caught.exception))
        C.open_store(state.parent / 'state').close()           # and a store of this version opens

    def test_open_store_and_register_refuse_with_their_own_errors(self) -> None:
        # DeepSeek r2, L3.
        a_file = self.folder / 'a-file'
        a_file.write_text('x')
        for state_dir in (None, b'/var/tmp', a_file, self.folder / 'missing'):
            with self.subTest(state_dir=state_dir):
                with self.assertRaises(D.StoreError) as caught:
                    C.open_store(state_dir)
                self.assertEqual(str(caught.exception), 'state_dir_not_a_folder')
        with self.assertRaises(TypeError) as caught:
            C.register(None)
        self.assertEqual(str(caught.exception), 'hooks')

    def test_a_refused_option_leaves_the_hooks_as_they_were(self) -> None:
        # Sol r2, later: a registration refused for an option can be made again on the same hooks.
        hooks = H.Hooks()
        with self.assertRaisesRegex(ValueError, 'page'):
            C.register(hooks, page=0)
        C.register(hooks, page=1)

    def test_log_id_of_refuses_a_run_id_that_is_not_text(self) -> None:
        # DeepSeek r2, L4.
        for run_id in (None, '', 5):
            with self.subTest(run_id=run_id):
                with self.assertRaisesRegex(TypeError, 'run_id'):
                    C.log_id_of(run_id, {'daemon_row': None, 'control_record': None})

    def test_a_session_id_too_long_to_be_a_log_id_logs_under_the_run(self) -> None:
        # DeepSeek r2, L1 (third guard).
        self.put('long', session_id='ses-' + 'x' * 61)
        self.drain()
        self.assertEqual([e['session_id'] for e in self.changed(RUN)], ['run:' + RUN])

    def test_a_run_in_a_page_and_gone_by_its_read_is_not_logged(self) -> None:
        # DeepSeek r2, L1 (second guard): no all-null image for a run the registry does not hold.
        real = self.registry.run_ids

        def a_ghost_first(after, limit):
            return ['run-ghost'] if after is None else real(after, limit)
        with mock.patch.object(self.registry, 'run_ids', a_ghost_first):
            self.drain()
        self.assertEqual((self.store.events, self.reader.ready), ([], True))

    def test_start_refuses_a_context_with_no_store(self) -> None:
        # DeepSeek r2, L1 (first guard).
        context = self.context()
        context.store = None
        service = C.ChangeReader(self.marker)
        with self.assertRaisesRegex(RuntimeError, 'needs the event store'):
            service.start(context)
        self.assertIsNone(service._thread)

    def test_a_finished_thread_is_owned_until_stop(self) -> None:
        # Sol r2's F10 probe.
        service = C.ChangeReader(self.marker)
        service._run = lambda stopping=None: None
        service.start(self.context())
        service._thread.join(BOUND_S)
        with self.assertRaisesRegex(RuntimeError, 'already started'):
            service.start(self.context())
        service.stop(self.context())
        service.start(self.context())
        service.stop(self.context())
        self.assertIsNone(service._thread)

    def test_a_control_text_nested_past_the_json_readers_own_limit_is_listed(self) -> None:
        # Sol r2's F1 probe: 1,500 levels.
        self.registry.control_put(RUN, '{"a":' * 1500 + '0' + '}' * 1500)
        self.put('healthy', RUN2)
        with self.assertLogs(C.LOG, 'ERROR'):
            self.drain()
        self.assertIn(RUN, self.reader.unlogged)
        self.assertEqual(self.notes(RUN2), ['healthy'])

    def test_a_control_text_nested_past_what_the_json_reader_can_follow_is_listed_and_the_scan_goes_on(self) -> None:
        # Devin's thread on #403 (changes.py:135): 100,000 levels made json.loads raise RecursionError, which nothing
        # caught; every step raised, the first pass never ended and the healthy run after it was never read.
        deep = '{"a":' * 100000 + '0' + '}' * 100000
        self.registry.control_put(RUN, deep)
        self.put('healthy', RUN2)
        with self.assertLogs(C.LOG, 'ERROR'):
            self.drain()
        self.assertTrue(self.reader.ready, 'the first pass ended')
        self.assertEqual(self.reader.unlogged, {RUN: 'control_text_not_json:depth'})
        self.assertEqual(self.unlogged_table(), {RUN: 'control_text_not_json:depth'})
        self.assertEqual(self.notes(RUN2), ['healthy'])
        self.registry.control_put(RUN3, deep)                    # and a run read by its mark, after the first pass
        with self.assertLogs(C.LOG, 'ERROR'):
            self.drain()
        self.assertEqual(self.reader.unlogged[RUN3], 'control_text_not_json:depth')

    def test_the_lane_tables_checks_hold(self) -> None:
        # Sol r2's coverage guard: the CHECKs on log_id and route_logged.
        for log_id, route in (('invalid-log', 0), ('run:run-check', 2)):
            with self.subTest(log_id=log_id, route=route):
                with self.assertRaises(sqlite3.IntegrityError):
                    with self.store.transaction() as tx:
                        tx.execute('INSERT INTO events_run_images VALUES (?, ?, ?, ?)', ('run-check', log_id, 'a' * 64, route))

    def test_a_deferred_runs_wait_doubles_up_to_the_scan_interval(self) -> None:
        # Sol r2's coverage guard: the doubling and the cap of a run's own backoff.
        self.registry.add(RUN, 'boot-1')
        waits = []
        with mock.patch.object(self.registry, 'snapshot', side_effect=OSError('unreadable row')):
            with self.assertLogs(C.LOG, 'WARNING'):
                self.reader.step()
                self.reader.step()
                for _ in range(12):
                    self.now[0] = self.reader._deferred[RUN][1]
                    self.reader.step()
                    waits.append(round(self.reader._deferred[RUN][1] - self.now[0], 6))
        self.assertEqual((waits[:3], waits[-2:]), ([0.2, 0.4, 0.8], [60.0, 60.0]))

    # -- round 3 (DeepSeek's read of 1290fbce)

    def test_a_run_the_registry_refuses_to_read_is_listed_until_it_is_read(self) -> None:
        # DeepSeek r3, M: ready and unlogged said the log was complete while a refused run was in neither.
        self.put('a', RUN)
        self.put('b', RUN2)
        real = self.registry.snapshot

        def refuse_run2(run_id):
            if run_id == RUN2:
                raise OSError('unreadable row')
            return real(run_id)
        with mock.patch.object(self.registry, 'snapshot', refuse_run2), self.assertLogs(C.LOG, 'WARNING'):
            for _ in range(10):
                self.reader.step()
                if self.reader.ready:
                    break
        self.assertTrue(self.reader.ready)
        self.assertEqual((self.reader.unlogged, self.unlogged_table()), ({RUN2: 'registry_refused'},) * 2)
        self.assertEqual(self.notes(RUN2), [], 'the log lacks it, and unlogged says so')
        restarted = self.new_reader(page=1)
        self.assertTrue(restarted.step())                      # the scan state, then a page of one run: RUN alone
        self.assertEqual(restarted.unlogged, {RUN2: 'registry_refused'}, 'a restart reads the listing back')
        self.drain()
        self.assertEqual((self.reader.unlogged, self.unlogged_table(), self.notes(RUN2)), ({}, {}, ['b']))

    def test_a_refused_run_whose_listing_fails_ends_the_step_and_is_listed_later(self) -> None:
        # DeepSeek r3, M: listing a run the registry refused is a transaction, and can fail like any other.
        self.drain()
        self.put('a')
        self.put('b', RUN2)
        real, reads = self.registry.snapshot, []

        def refuse_run(run_id):
            reads.append(run_id)
            if run_id == RUN:
                raise OSError('unreadable row')
            return real(run_id)
        self.store.fail_commit = True
        with mock.patch.object(self.registry, 'snapshot', refuse_run), self.assertLogs(C.LOG, 'WARNING'):
            self.assertFalse(self.reader.step())
            self.assertEqual((reads, self.reader.unlogged, self.reader._pending), ([RUN], {}, {RUN, RUN2}))
            self.store.fail_commit = False
            self.now[0] = self.reader._wait_until
            refusals, _, reason = self.reader._deferred[RUN]
            self.reader._deferred[RUN] = (refusals, self.now[0] + 50, reason)   # its own wait outlasts the store's
            self.reader._scan_wanted = True
            self.reader.step()
        self.assertEqual(reads.count(RUN), 1, 'the scan met it before its own wait had passed and did not read it')
        self.assertEqual((self.reader.unlogged, self.unlogged_table()), ({RUN: 'registry_refused'},) * 2, 'but listed it')
        self.assertEqual(self.notes(RUN2), ['b'])
        self.drain()
        self.assertEqual((self.reader.unlogged, self.unlogged_table(), self.notes()), ({}, {}, ['a']))

    def test_a_poll_interval_too_large_for_a_float_is_read_by_its_sign(self) -> None:
        # DeepSeek r3, L1: float(10**400) raised OverflowError out of step().
        for value, expected in ((10 ** 400, 60.0), (-10 ** 400, C.MIN_POLL_S)):
            with self.subTest(positive=value > 0):
                self.marker.poll_interval_s = value
                self.assertEqual(self.reader._poll_s(), expected)
        self.marker.poll_interval_s = 10 ** 400
        self.registry.add(RUN, 'boot-1')
        self.drain()
        self.assertEqual(len(self.changed(RUN)), 1)

    def test_a_listed_run_back_at_its_last_logged_image_leaves_the_listing_and_logs_nothing(self) -> None:
        # DeepSeek r3, L2: the listing leaves in the transaction that reads a typed image, new to the log or not.
        self.put('A')
        self.drain()
        self.registry.control_put(RUN, 'not json')
        with self.assertLogs(C.LOG, 'ERROR'):
            self.drain()
        self.assertEqual(self.unlogged_table(), {RUN: 'control_text_not_json'})
        self.put('A')
        self.drain()
        self.assertEqual((self.unlogged_table(), self.reader.unlogged, self.notes()), ({}, {}, ['A']))

    def test_a_listed_run_the_registry_holds_nothing_for_leaves_the_listing(self) -> None:
        # DeepSeek r3, L2: nothing is missing from the log for a run the registry does not hold and it never saw.
        with self.store.transaction() as tx:
            tx.execute("INSERT INTO events_unlogged (run_id, code) VALUES ('run-ghost', 'control_text_not_json')")
        self.drain()
        self.assertEqual(self.reader.unlogged, {'run-ghost': 'control_text_not_json'})
        self.marker.mark('run-ghost')
        self.drain()
        self.assertEqual((self.unlogged_table(), self.reader.unlogged, self.store.events), ({}, {}, []))

    def test_a_store_another_reader_has_started_is_refused(self) -> None:
        # DeepSeek r3, L3: two readers on one store could leave the log's last image behind the registry's.
        first, second = C.ChangeReader(self.marker), C.ChangeReader(H.ChangeMarker())
        first._run = second._run = lambda stopping=None: None
        context = self.context()
        first.start(context)
        with self.assertRaisesRegex(RuntimeError, 'another change reader'):
            second.start(context)
        self.assertIsNone(second._thread)
        with self.assertRaisesRegex(RuntimeError, 'another change reader'):
            second.bind(self.registry, self.store)
        first.stop(context)
        second.start(context)
        second.stop(context)
        first.bind(self.registry, self.store)                  # free again after second's stop

    def other_store(self) -> D.FakeStore:
        other = D.FakeStore()
        other.register_tables(C.LANE, C.LANE_VERSION, C.LANE_DDL)
        self.addCleanup(other.close)
        return other

    def test_a_started_reader_refuses_a_bind_to_another_registry_or_store(self) -> None:
        # Sol, round 3 at 45eedbcf, required 1: a bind while started moved the reader's writes to a store whose claim
        # it did not hold, so a second reader could start there too; both logged, and the log ended B, A while the
        # registry held B. It replaces the case that accepted that bind (DeepSeek r4, LOW-2), whose release check
        # stays below.
        first, second = C.ChangeReader(self.marker), C.ChangeReader(H.ChangeMarker())
        first._run = second._run = lambda stopping=None: None
        context, other, other_registry = self.context(), self.other_store(), mock.Mock(spec=[])
        first.start(context)
        for registry, store in ((self.registry, other), (other_registry, self.store), (other_registry, other)):
            with self.subTest(registry=registry, store=store):
                with self.assertRaisesRegex(RuntimeError, 'bind cannot change its registry or store until stop'):
                    first.bind(registry, store)
                self.assertIs(first._registry, self.registry)
                self.assertIs(first._store, self.store)
        first.bind(self.registry, self.store)                   # the same two: nothing changes
        with self.assertRaisesRegex(RuntimeError, 'another change reader'):
            second.start(context)                               # first's claim stays on the store it started with
        first.stop(context)
        second.start(context)                                   # given up by the stop
        second.stop(context)
        first.bind(self.registry, other)                        # stopped: bind may change both again

    def test_an_interruption_right_after_start_takes_the_claim_gives_it_back(self) -> None:
        # DeepSeek r5 LOW-2 (changes.py:308-309, 330 at 1ff2ee1a): an interruption between the claim and remembering it
        # left the store claimed with nothing to release it, and every later start on it was refused.
        reader, later = C.ChangeReader(self.marker), C.ChangeReader(H.ChangeMarker())
        reader._run = later._run = lambda stopping=None: None
        context, real = self.context(), C._claim

        def claim_then_interrupted(store, who, *, take):
            real(store, who, take=take)
            if take and who is reader:
                raise KeyboardInterrupt
        with mock.patch.object(C, '_claim', claim_then_interrupted):
            with self.assertRaises(KeyboardInterrupt):
                reader.start(context)
        self.assertIsNone(reader._claimed)
        later.start(context)                                    # the store is free: no stop was needed
        later.stop(context)
        reader.stop(context)                                    # and a stop after it is safe

    def test_a_start_that_fails_while_it_binds_gives_the_claim_back(self) -> None:
        # DeepSeek r5 LOW-2: the same promise at the binding, the next step after the claim.
        reader, later = C.ChangeReader(self.marker), C.ChangeReader(H.ChangeMarker())
        reader._run = later._run = lambda stopping=None: None
        context, real = self.context(), C._claim

        def refused_when_binding(store, who, *, take):
            if not take and who is reader:
                raise KeyboardInterrupt
            real(store, who, take=take)
        with mock.patch.object(C, '_claim', refused_when_binding):
            with self.assertRaises(KeyboardInterrupt):
                reader.start(context)
        later.start(context)
        later.stop(context)
        reader.bind(self.registry, self.other_store())          # not started: bind may change both

    def test_a_bind_on_another_thread_while_start_is_under_way_is_refused(self) -> None:
        # DeepSeek r5 LOW-1 (changes.py:296-297 vs 309 at 1ff2ee1a): a bind that landed after start's claim and binding
        # but before its thread was owned moved the started reader to a store whose claim it did not hold.
        reader, other = C.ChangeReader(self.marker), self.other_store()
        reader._run = lambda stopping=None: None
        context, reset = self.context(), reader._reset
        paused, go, outcome = threading.Event(), threading.Event(), []

        def slow_reset():
            paused.set()
            go.wait(BOUND_S)
            reset()
        reader._reset = slow_reset
        starter = threading.Thread(target=reader.start, args=(context,))
        starter.start()
        self.addCleanup(starter.join, BOUND_S)
        self.assertTrue(paused.wait(BOUND_S))
        try:
            reader.bind(self.registry, other)
            outcome.append('moved')
        except RuntimeError as exc:
            outcome.append(str(exc))
        go.set()
        starter.join(BOUND_S)
        self.assertEqual(outcome, ['the change reader is started: bind cannot change its registry or store until stop'])
        self.assertIs(reader._store, self.store)
        later = C.ChangeReader(H.ChangeMarker())
        later._run = lambda stopping=None: None
        later.start(mock.Mock(registry=self.registry, store=other))   # other was never this reader's
        later.stop(mock.Mock(registry=self.registry, store=other))
        reader.stop(context)

    def test_a_bind_under_way_when_start_begins_is_finished_first(self) -> None:
        # DeepSeek r5 LOW-1, the other order: a bind that passed its check before start began must not land after
        # start's binding. Start waits for it, then binds last.
        reader, other = C.ChangeReader(self.marker), self.other_store()
        reader._run = lambda stopping=None: None
        context, real, calls = self.context(), reader._bind, []
        entered, go = threading.Event(), threading.Event()

        def slow_first_bind(registry, store):
            calls.append(store)
            if len(calls) == 1:
                entered.set()
                go.wait(BOUND_S)
            real(registry, store)
        reader._bind = slow_first_bind
        binder = threading.Thread(target=reader.bind, args=(self.registry, other))
        binder.start()
        self.addCleanup(binder.join, BOUND_S)
        self.assertTrue(entered.wait(BOUND_S), 'bind goes through _bind')
        starter = threading.Thread(target=reader.start, args=(context,))
        starter.start()
        self.addCleanup(starter.join, BOUND_S)
        time.sleep(NOT_YET_S)
        self.assertEqual(len(calls), 1, 'start waits for the bind under way')
        go.set()
        binder.join(BOUND_S)
        starter.join(BOUND_S)
        self.assertIs(reader._store, self.store, 'the start binds last')
        reader.stop(context)

    def test_a_store_the_claim_map_cannot_hold_is_refused_before_any_mark(self) -> None:
        # Grok r6, R1 (changes.py:322-328, 350 at c14a461d; Devin's thread on 326-328): such a store set the mark, the
        # release raised the same TypeError, a later start was refused as already started, and stop never returned.
        class Slots:
            __slots__ = ()

        class Unhashable:
            __hash__ = None

        for bad in ({}, [], 1, Slots(), Unhashable()):
            with self.subTest(store=type(bad).__name__):
                reader = C.ChangeReader(self.marker)
                reader._run = lambda stopping=None: None
                with self.assertRaisesRegex(TypeError, 'hashable and weak-referenceable'):
                    reader.start(mock.Mock(registry=self.registry, store=bad))
                self.assertIsNone(reader._claimed)
                C._release(bad, reader)                                      # nothing was claimed: nothing raised
                with self.assertRaisesRegex(TypeError, 'hashable and weak-referenceable'):
                    reader.bind(self.registry, bad)
                self.assertIsNot(reader._store, bad)
                reader.stop(mock.Mock(registry=self.registry, store=bad))    # returns: nothing to give up
                reader.start(self.context())                                 # a real store: not "already started"
                reader.stop(self.context())

    def test_an_interruption_on_the_release_try_does_not_keep_the_claim_lock(self) -> None:
        # Grok r7 on #403, R1 (changes.py _release): the ``try`` was the first line of ``with _CLAIMS_LOCK``.
        # An interruption on that line returned with the process-wide claim lock still held, and stop did not return.
        lines, start = inspect.getsourcelines(C._release)
        try_at = next(start + offset for offset, line in enumerate(lines) if line.strip().startswith('try:'))
        reader = C.ChangeReader(self.marker)
        reader._run = lambda stopping=None: None
        context = self.context()
        store = context.store

        def bind_raises(registry, store_):
            raise RuntimeError('bind failed after the claim')

        reader._bind = bind_raises
        fired = []

        def tracer(frame, event, arg):
            if event == 'line' and frame.f_code is C._release.__code__ and frame.f_lineno == try_at and not fired:
                fired.append(frame.f_lineno)
                sys.settrace(None)
                frame.f_trace = None
                raise BaseException('injected at the release try')
            return tracer

        def release_a_stuck_lock() -> None:
            if C._CLAIMS_LOCK.locked():
                C._CLAIMS_LOCK.release()
            if reader._binding.locked():
                reader._binding.release()
            try:
                if C._CLAIMS.get(store) is reader:
                    del C._CLAIMS[store]
            except TypeError:
                pass
            reader._claimed = None

        self.addCleanup(release_a_stuck_lock)
        sys.settrace(tracer)
        try:
            with self.assertRaises(BaseException) as caught:
                reader.start(context)
        finally:
            sys.settrace(None)
        self.assertEqual(fired, [try_at])
        self.assertEqual(str(caught.exception), 'injected at the release try')
        self.assertFalse(C._CLAIMS_LOCK.locked())
        self.assertFalse(reader._binding.locked())
        reader.stop(context)
        self.assertIsNone(reader._claimed)
        self.assertNotIn(store, list(C._CLAIMS.keys()))
        later = C.ChangeReader(self.marker)
        later._run = lambda stopping=None: None
        later.start(context)
        later.stop(context)

    def test_a_start_whose_thread_never_started_refuses_a_rebind_until_its_stop(self) -> None:
        # Sol, round 3, required 1: the refusal holds from start to stop, a start that raised included.
        context, service, other = self.context(), C.ChangeReader(self.marker), self.other_store()
        with mock.patch.object(threading.Thread, 'start', side_effect=RuntimeError("can't start new thread")):
            with self.assertRaisesRegex(RuntimeError, "can't start new thread"):
                service.start(context)
        with self.assertRaisesRegex(RuntimeError, 'bind cannot change its registry or store until stop'):
            service.bind(self.registry, other)
        service.stop(context)
        later = C.ChangeReader(H.ChangeMarker())
        later._run = lambda stopping=None: None
        later.start(context)                                    # the failed start's claim was given up
        later.stop(context)
        service.bind(self.registry, other)

    def test_log_id_of_and_image_digest_refuse_what_is_not_a_post_image(self) -> None:
        # DeepSeek r3, L4.
        for image in ([1], None, 'text', {}, {'daemon_row': [], 'control_record': None},
                      {'daemon_row': 5, 'control_record': 'x'}, {'daemon_row': None, 'control_record': None, 'x': 1}):
            with self.subTest(image=image):
                with self.assertRaisesRegex(C.BadImage, 'not_a_post_image'):
                    C.log_id_of(RUN, image)
                with self.assertRaisesRegex(C.BadImage, 'not_a_post_image'):
                    C.image_digest(image)

    def test_a_clock_that_cannot_be_called_is_refused_with_the_options(self) -> None:
        # DeepSeek r3, L5: it was accepted, and step() then raised on the service thread.
        for clock in (None, 'later'):
            with self.subTest(clock=clock):
                with self.assertRaisesRegex(ValueError, 'monotonic'):
                    C.ChangeReader(self.marker, monotonic=clock)
        hooks = H.Hooks()
        with self.assertRaisesRegex(ValueError, 'monotonic'):
            C.register(hooks, monotonic=None)
        C.register(hooks)

    def test_a_store_refused_for_its_version_is_closed_and_refused_the_same_way_again(self) -> None:
        # DeepSeek r3, L6: without the close, the second open said store_in_use.
        state = self.folder / 'old-twice'
        state.mkdir()
        old = D.EventStore(str(state / C.STORE_FILE))
        old.register_tables(C.LANE, 1, C.LANE_DDL.rsplit('; CREATE TABLE events_unlogged', 1)[0])
        old.close()
        for attempt in (1, 2):
            with self.subTest(attempt=attempt):
                with self.assertRaisesRegex(D.StoreError, 'tables of version 1'):
                    C.open_store(state)

    def test_a_store_whose_declaration_is_interrupted_is_closed(self) -> None:
        # DeepSeek r3, L6: without the close, every later open of the folder said store_in_use.
        state = self.folder / 'interrupted'
        state.mkdir()
        with mock.patch.object(D.EventStore, 'register_tables', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                C.open_store(state)
        C.open_store(state).close()

    def test_a_value_from_the_marker_that_is_not_a_run_id_is_dropped_and_asks_for_a_scan(self) -> None:
        # DeepSeek r3, L7: one such value stopped the marked runs' slice for good.
        marker = OddMarker(self.marker)
        reader = C.ChangeReader(marker, monotonic=lambda: self.now[0])
        reader.bind(self.registry, self.store)
        self.drain(reader)
        marker.extra = [5, '', None]
        self.put('a')
        self.put('b', RUN2)
        with self.assertLogs(C.LOG, 'ERROR') as logged:
            reader.step()
        self.assertIn('3 values that are not run ids', logged.output[0])
        self.assertTrue(reader._scanning, 'a scan is asked for, as for a dropped mark')
        self.drain(reader)
        self.assertEqual((self.notes(), self.notes(RUN2), reader._pending), (['a'], ['b'], set()))


class RealStore(D.EventStore):
    """The real helper (WAL, ``synchronous=FULL``, its lock file), with the fake's read-only views for the
    assertions above and its ``fail_commit`` seam: the same code as the fake's, over this durable file."""

    fail_commit = False
    _before_commit = D.FakeStore._before_commit
    _reader = D.FakeStore._reader
    _read = D.FakeStore._read
    events = D.FakeStore.events
    rows = D.FakeStore.rows
    open_transactions = D.FakeStore.open_transactions


@unittest.skipUnless(POSIX, 'the daemon registry is Linux only')
class ChangeReaderOnTheRealStoreTest(ChangeReaderTest):
    """Every case above, against the real event store."""

    def new_store(self) -> RealStore:
        store = RealStore(self.folder / 'events.sqlite3')
        store.register_tables(C.LANE, C.LANE_VERSION, C.LANE_DDL)
        self.addCleanup(lambda: store.open_transactions or store.close())
        return store

    def test_the_store_here_is_the_durable_one(self) -> None:
        self.assertIs(type(self.store), RealStore)
        self.assertEqual(self.store._conn.execute('PRAGMA synchronous').fetchone(), (2,), 'FULL on its own connection')
        with self.store._reader() as conn:
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone(), ('wal',))
        self.assertTrue(os.path.exists(self.store.path + '.lock'), 'it holds the one-writer lock file')


if __name__ == '__main__':
    unittest.main()
