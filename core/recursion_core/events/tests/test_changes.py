"""The change reader (events/changes.py) against the daemon's real Registry and SPINE's real ChangeMarker.

The reader is driven one ``step()`` at a time with a clock the test moves, so nothing here waits on time
except to check that something has NOT happened yet. Every case runs twice: on the helper's fake (its own code,
durability off) and on the real event store in the test's folder (``RealStore``: WAL, synchronous FULL, its
lock file), the paused-consumer cases among them. What the helper itself guarantees under a crash is in
test_db.py and test_durability.py; the reader's own crash points, by kill -9, are in test_reader_crash.py.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
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
            'run-bigint': 'NoCanonicalText:integer', 'run-inf': 'NoCanonicalText:number',
            'run-surrogate': 'NoCanonicalText:surrogate', 'run-nan': 'control_text_not_json:NaN',
            'run-notjson': 'control_text_not_json', 'run-list': 'control_text_not_an_object',
            'run-deep': 'ValueError:value_too_deep'})
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
