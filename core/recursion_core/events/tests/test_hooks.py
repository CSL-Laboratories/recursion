"""The events registrant (events/hooks.py): composition sets the event store.

With no store, a command whose handler is registered answers ``unsupported`` /
``store_absent`` and the handler is not called. ``hooks.compose()`` loads
``recursion_core.events.hooks``, which sets the ``event_store`` factory.
Opening that store is what lets those commands answer the handler.

The handlers of session, approval and workspace methods belong to those lanes.
This tree does not ship them, and the SESSIONS registrant registers nothing on
purpose, so on the composed table those methods stay ``unsupported`` /
``not_implemented``. A stand-in handler in this file is only how the test
shows the store the registrant opened is the one the dispatcher uses.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from recursion_core.control.hooks import OWNER, HookError, Hooks, compose
from recursion_core.control.methods import INSTALL_TENANT
from recursion_core.events import db as events_db
from recursion_core.events.db import EventStore, StoreError

POSIX = events_db.fcntl is not None
CMD = 'command-0001'

# Command methods named by the entry. Each keeps a receipt, so with a handler
# and no store the dispatcher answers store_absent and calls nothing.
COMMANDS = (
    'session.stop',
    'session.steer',
    'session.queue',
    'session.interrupt',
    'approval.open',
    'approval.answer',
    'workspace.checkpoint',
    'workspace.restore',
    'workspace.publish',
)

# Reads in the same families. They keep no receipt. With no handler they stay
# not_implemented whether or not a store is composed.
READS = (
    'approval.list',
    'workspace.diff',
)


def _events_hooks():
    from recursion_core.events import hooks as events_hooks
    return events_hooks


class EventsRegistrantTest(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.mkdtemp(prefix='events-registrant-')
        self.addCleanup(shutil.rmtree, self.folder, True)

    def test_with_no_store_a_registered_command_answers_unsupported_and_is_not_called(self) -> None:
        calls = []

        def handler(ctx, params):
            calls.append(params.get('command_id'))
            return {'code': 'ok'}

        bare = Hooks()
        for name in COMMANDS:
            bare.methods.register(name, handler, tenant=INSTALL_TENANT)
        bare.freeze()
        self.assertIsNone(bare.store)
        for name in COMMANDS:
            with self.subTest(name=name):
                answer = bare.methods.dispatch(name, {'command_id': CMD}, OWNER, None)
                self.assertEqual(answer, {'code': 'unsupported', 'detail': 'store_absent'})
        self.assertEqual(calls, [])
        for name in READS:
            with self.subTest(name=name):
                answer = bare.methods.dispatch(name, {}, OWNER, None)
                self.assertEqual(answer, {'code': 'unsupported', 'detail': 'not_implemented'})

    def test_compose_sets_the_event_store_and_foreign_methods_stay_unimplemented(self) -> None:
        if not POSIX:
            self.skipTest('the real store needs the file lock this platform does not have')
        composed = compose()
        store = composed.open_store(self.folder)
        self.assertIsInstance(store, EventStore, 'compose sets no event store: events/hooks.py is absent')
        self.addCleanup(store.close)
        self.assertIs(composed.store, store)
        self.assertIs(composed.methods.store, store)
        self.assertTrue(composed.gate.enabled)
        self.assertIsNone(composed.changes)
        self.assertFalse(composed.start_on)
        self.assertEqual(composed.methods.registered(), ())
        self.assertEqual(os.path.basename(store._path), 'events.sqlite3')
        self.assertEqual(stat.S_IMODE(os.stat(store._path).st_mode) & 0o077, 0)
        events_hooks = _events_hooks()
        self.assertIs(composed._single['event_store'], events_hooks.open_store)
        for name in (*COMMANDS, *READS):
            with self.subTest(name=name):
                answer = composed.methods.dispatch(name, {'command_id': CMD}, OWNER, None)
                self.assertEqual(answer, {'code': 'unsupported', 'detail': 'not_implemented'})

    def test_a_command_handler_answers_once_the_registrant_has_opened_the_store(self) -> None:
        if not POSIX:
            self.skipTest('the real store needs the file lock this platform does not have')
        events_hooks = _events_hooks()
        calls = []

        def handler(ctx, params):
            self.assertIsNotNone(ctx.tx)
            calls.append(params['command_id'])
            return {'code': 'ok'}

        live = Hooks()
        events_hooks.register(live)
        for name in COMMANDS:
            live.methods.register(name, handler, tenant=INSTALL_TENANT)
        live.freeze()
        store = live.open_store(self.folder)
        self.assertIsInstance(store, EventStore)
        self.addCleanup(store.close)
        self.assertIs(live.methods.store, store)
        for name in COMMANDS:
            with self.subTest(name=name):
                answer = live.methods.dispatch(name, {'command_id': CMD}, OWNER, None)
                self.assertEqual(answer, {'code': 'ok'})
                self.assertNotEqual(answer['code'], 'unsupported')
        self.assertEqual(calls, [CMD] * len(COMMANDS))
        again = live.methods.dispatch('session.stop', {'command_id': CMD}, OWNER, None)
        self.assertEqual(again, {'code': 'ok'})
        self.assertEqual(calls, [CMD] * len(COMMANDS), 'a replay calls nothing')
        conflict = live.methods.dispatch(
            'session.stop', {'command_id': CMD, 'text': 'other'}, OWNER, None)
        self.assertEqual(conflict, {'code': 'conflict', 'detail': 'command_id_reused'})
        self.assertEqual(calls, [CMD] * len(COMMANDS))
        with store.transaction() as tx:
            kept = tx.receipt_get(INSTALL_TENANT, 'session.stop', CMD)
        self.assertEqual(kept[1], {'code': 'ok'})
        for name in READS:
            with self.subTest(name=name):
                answer = live.methods.dispatch(name, {}, OWNER, None)
                self.assertEqual(answer, {'code': 'unsupported', 'detail': 'not_implemented'})

    def test_a_second_registration_is_refused_and_a_bad_hooks_object_is_refused(self) -> None:
        events_hooks = _events_hooks()
        live = Hooks()
        events_hooks.register(live)
        with self.assertRaises(HookError) as caught:
            events_hooks.register(live)
        self.assertIn('event_store', str(caught.exception))
        for bad in (None, object()):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(TypeError) as caught:
                    events_hooks.register(bad)
                self.assertEqual(str(caught.exception), 'hooks')

    def test_a_state_directory_that_is_not_a_folder_is_refused(self) -> None:
        events_hooks = _events_hooks()
        missing = os.path.join(self.folder, 'missing')
        sample = os.path.join(self.folder, 'a-file')
        with open(sample, 'wb') as handle:
            handle.write(b'x')
        for bad in (None, b'/tmp', 3, '', missing, sample):
            with self.subTest(bad=bad if isinstance(bad, (str, bytes)) else type(bad).__name__):
                with self.assertRaises(StoreError) as caught:
                    events_hooks.open_store(bad)
                self.assertEqual(str(caught.exception), 'state_dir_not_a_folder')

    def test_a_damaged_file_is_refused_and_left_as_it_is_and_an_interruption_propagates(self) -> None:
        if not POSIX:
            self.skipTest('the real store needs the file lock this platform does not have')
        events_hooks = _events_hooks()
        path = os.path.join(self.folder, 'events.sqlite3')
        payload = b'this is not a database file at all'
        with open(path, 'wb') as handle:
            handle.write(payload)
        os.chmod(path, 0o600)
        with self.assertRaises(StoreError) as caught:
            events_hooks.open_store(self.folder)
        self.assertEqual(str(caught.exception), 'not_an_event_store')
        with open(path, 'rb') as handle:
            self.assertEqual(handle.read(), payload)
        with mock.patch('recursion_core.events.hooks.EventStore', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                events_hooks.open_store(self.folder)
        with mock.patch('recursion_core.events.hooks.EventStore', side_effect=StoreError('store_corrupt')):
            with self.assertRaises(StoreError) as caught:
                events_hooks.open_store(self.folder)
            self.assertEqual(str(caught.exception), 'store_corrupt')

    def test_a_second_opener_of_the_same_store_is_refused_and_the_first_still_serves(self) -> None:
        if not POSIX:
            self.skipTest('the real store needs the file lock this platform does not have')
        events_hooks = _events_hooks()
        first = events_hooks.open_store(self.folder)
        self.addCleanup(first.close)
        with self.assertRaises(StoreError) as caught:
            events_hooks.open_store(self.folder)
        self.assertEqual(str(caught.exception), 'store_in_use')
        with first.transaction() as tx:
            self.assertIsNone(tx.receipt_get(INSTALL_TENANT, 'session.stop', CMD))
        other = tempfile.mkdtemp(prefix='events-registrant-other-')
        self.addCleanup(shutil.rmtree, other, True)
        second = events_hooks.open_store(other)
        self.addCleanup(second.close)
        self.assertIsInstance(second, EventStore)
        self.assertNotEqual(first._path, second._path)


if __name__ == '__main__':
    unittest.main()
