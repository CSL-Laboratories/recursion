"""The change reader (events/changes.py) against the daemon's real Registry and SPINE's real ChangeMarker.

The reader is driven one ``step()`` at a time with a clock the test moves, so nothing here waits on time
except to check that something has NOT happened yet. The store is the helper's fake (its own code, durability
off); what the helper itself guarantees under a crash is in test_db.py and test_durability.py.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
import threading
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
        """Steps until no scan is in progress and nothing is pending."""
        reader = reader or self.reader
        for _ in range(1000):
            if not reader.step() and not reader._pending:
                return
        self.fail('the reader did not settle')

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
        self.store.fail_commit = False
        self.drain()
        self.assertEqual([e['run_id'] for e in self.changed()], [RUN])

    def test_a_registry_that_refuses_a_read_keeps_the_run_pending(self) -> None:
        self.drain()
        self.registry.control_put(RUN, record('kept'))
        with mock.patch.object(self.registry, 'snapshot', side_effect=daemon_module.RegistryIntegrityError('closed')):
            with self.assertLogs(C.LOG, 'WARNING'):
                self.reader.step()
        self.assertEqual((self.changed(), self.reader._pending), ([], {RUN}))
        self.drain()
        self.assertEqual([e['run_id'] for e in self.changed()], [RUN])

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


if __name__ == '__main__':
    unittest.main()
