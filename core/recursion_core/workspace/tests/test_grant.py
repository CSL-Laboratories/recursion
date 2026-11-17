"""Per-run access to a folder (workspace/grant.py): the two published functions and the per-run record.

``grant(path, uid)`` and ``revoke(grant_id)`` are what lane STOP builds on; their signatures are pinned here.
The record is written through the event store's transaction helper; until lane EVENTS' store is on this
lane's base the tests use lane SPINE's ``FakeStore``, the one shared fake of that helper.
"""
from __future__ import annotations

import inspect
import os
import pathlib
import shutil
import sqlite3
import tempfile
import unittest

from recursion_core.control.tests.test_seams import FakeStore
from recursion_core.workspace import grant as G
from recursion_core.workspace.tests.published import fits

POSIX = os.name == 'posix'


class PublishedSignaturesTest(unittest.TestCase):

    def test_grant_takes_a_path_and_a_uid_and_revoke_a_grant_id(self) -> None:
        self.assertEqual(list(inspect.signature(G.grant).parameters), ['path', 'uid'])
        self.assertEqual(list(inspect.signature(G.revoke).parameters), ['grant_id'])
        for function in (G.grant, G.revoke):
            for parameter in inspect.signature(function).parameters.values():
                self.assertEqual(parameter.kind, parameter.POSITIONAL_OR_KEYWORD)
                self.assertIs(parameter.default, parameter.empty)

    def test_every_code_is_a_code_word(self) -> None:
        for code in G.CODES:
            self.assertRegex(code, r'^[a-z][a-z0-9_]{0,63}$')
        with self.assertRaises(ValueError):
            G.GrantRefused('anything_else')


@unittest.skipUnless(POSIX, 'the workspace checks are POSIX: the daemon runs on Linux')
class GrantTest(unittest.TestCase):

    def setUp(self) -> None:
        self.root = pathlib.Path(os.path.realpath(tempfile.mkdtemp(prefix='ws-grant-')))
        self.addCleanup(shutil.rmtree, self.root)
        self.folder = self.root / 'folder'
        self.folder.mkdir()
        self.me = os.geteuid()

    def refused(self, call, code, reason='') -> None:
        with self.assertRaises(G.GrantRefused) as caught:
            call()
        self.assertEqual((caught.exception.code, caught.exception.reason), (code, reason))

    def test_the_operators_own_grant_has_an_id_and_asking_again_gives_the_same(self) -> None:
        first = G.grant(str(self.folder), self.me)
        self.assertRegex(first, r'^wsg-[0-9a-f]{32}$')
        self.assertEqual(G.grant(str(self.folder), self.me), first)
        self.assertEqual(G.grant(self.folder, self.me), first)
        self.assertEqual(first, G.grant_id_of(self.folder, self.me))

    def test_the_id_is_the_folders_as_it_really_is(self) -> None:
        link = self.root / 'link'
        link.symlink_to(self.folder)
        self.assertEqual(G.grant(str(link), self.me), G.grant(str(self.folder), self.me))
        other = self.root / 'other'
        other.mkdir()
        self.assertNotEqual(G.grant(str(other), self.me), G.grant(str(self.folder), self.me))
        self.assertNotEqual(G.grant_id_of(self.folder, self.me), G.grant_id_of(self.folder, self.me + 1))

    def test_a_grant_changes_nothing_on_the_folder(self) -> None:
        def seen():
            info = os.stat(self.folder)
            names = os.listxattr(self.folder) if hasattr(os, 'listxattr') else []
            return info.st_mode, info.st_uid, info.st_gid, info.st_ctime_ns, sorted(names)
        before = seen()
        G.revoke(G.grant(str(self.folder), self.me))
        self.assertEqual(seen(), before)

    def test_revoke_is_idempotent(self) -> None:
        grant_id = G.grant(str(self.folder), self.me)
        self.assertIsNone(G.revoke(grant_id))
        self.assertIsNone(G.revoke(grant_id))

    def test_revoke_refuses_what_is_not_a_grant_id(self) -> None:
        for value in (None, '', 'wsg-', 'wsg-' + 'g' * 32, 'wsg-' + 'a' * 31, 'wsg-' + 'A' * 32, 'xsg-' + 'a' * 32,
                      5, 'wsg-' + 'a' * 32 + '\n'):
            self.refused(lambda: G.revoke(value), 'grant_unknown')

    def test_a_uid_that_is_not_a_whole_number_from_one_is_refused(self) -> None:
        for uid in (0, -1, True, False, '1000', 1.0, None):
            self.refused(lambda: G.grant(str(self.folder), uid), 'uid_invalid')

    def test_another_user_is_refused_until_the_access_list_is_built(self) -> None:
        other = self.me + 1 if self.me + 1 != 0 else self.me + 2
        self.refused(lambda: G.grant(str(self.folder), other), 'grant_unsupported')

    def test_a_folder_the_operator_may_not_be_given_is_refused_with_its_reason(self) -> None:
        self.refused(lambda: G.grant('relative', self.me), 'path_invalid', 'not_absolute')
        self.refused(lambda: G.grant(str(self.root / 'missing'), self.me), 'path_invalid', 'not_found')
        (self.root / 'file').write_text('x')
        self.refused(lambda: G.grant(str(self.root / 'file'), self.me), 'path_invalid', 'not_a_directory')
        if self.me != 0:
            self.refused(lambda: G.grant('/usr', self.me), 'path_invalid', 'not_owned')

    def test_a_folder_named_with_any_character_may_be_granted(self) -> None:
        spaced = self.root / 'my project'
        spaced.mkdir()
        self.assertEqual(G.grant(str(spaced), self.me), G.grant_id_of(spaced, self.me))

    def test_a_worktree_under_the_state_directory_may_be_granted(self) -> None:
        tree = self.root / 'state' / 'worktrees' / 'run-1'
        tree.mkdir(parents=True)
        self.assertRegex(G.grant(str(tree), self.me), r'^wsg-[0-9a-f]{32}$')


class LedgerTest(unittest.TestCase):

    def setUp(self) -> None:
        self.store = FakeStore()
        G.Ledger.declare(self.store)

    def granted(self, run_id, path, **options):
        with self.store.transaction() as tx:
            return G.Ledger.granted(tx, run_id, path, **options)

    def revoked(self, run_id):
        with self.store.transaction() as tx:
            return G.Ledger.revoked(tx, run_id)

    def unrevoked(self):
        with self.store.transaction() as tx:
            return G.Ledger.unrevoked(tx)

    def test_the_table_is_the_lanes_own_name(self) -> None:
        self.assertEqual(G.TABLE, 'workspace_grant')
        self.assertTrue(G.TABLE.startswith('workspace_'))
        self.assertIn(f'CREATE TABLE {G.TABLE} ', G.DDL)

    def test_a_grant_then_a_revoke_bracket_the_run_in_its_log(self) -> None:
        self.assertIsNotNone(self.granted('run-1', '/home/dev/projects/atlas'))
        self.assertIsNotNone(self.revoked('run-1'))
        self.assertEqual(self.store.events, [
            {'session_id': 'run:run-1', 'run_id': 'run-1', 'turn_id': None, 'type': 'workspace.grant',
             'source': 'core', 'trust': 'trusted', 'payload': {'path': '/home/dev/projects/atlas', 'run_id': 'run-1'}},
            {'session_id': 'run:run-1', 'run_id': 'run-1', 'turn_id': None, 'type': 'workspace.revoke',
             'source': 'core', 'trust': 'trusted', 'payload': {'path': '/home/dev/projects/atlas', 'run_id': 'run-1'}}])

    def rows(self):
        with self.store.transaction() as tx:
            return [tuple(row) for row in tx.execute(
                'SELECT run_id, log_id, path, granted_seq, revoked_seq FROM workspace_grant ORDER BY run_id')]

    def test_a_grant_recorded_again_while_it_stands_appends_nothing(self) -> None:
        self.assertIsNotNone(self.granted('run-1', '/p'))
        self.assertIsNone(self.granted('run-1', '/p'))
        self.assertEqual([event['type'] for event in self.store.events], ['workspace.grant'])

    def test_the_seqs_answered_are_the_events_and_the_row_keeps_them(self) -> None:
        self.assertEqual(self.granted('run-a', '/a'), 1)
        self.assertEqual(self.revoked('run-a'), 2)
        self.assertEqual(self.granted('run-b', '/b', log_id='ses-x'), 3)
        self.assertEqual(self.rows(), [('run-a', 'run:run-a', '/a', 1, 2), ('run-b', 'ses-x', '/b', 3, None)])

    def test_a_grant_after_the_revoke_is_a_new_grant_and_the_run_stands_unrevoked_again(self) -> None:
        # A start that failed after its grant gave it back; the seam retries it under the same run id.
        self.assertEqual(self.granted('run-1', '/p', log_id='ses-s'), 1)
        self.assertEqual(self.revoked('run-1'), 2)
        self.assertEqual(self.unrevoked(), [])
        self.assertEqual(self.granted('run-1', '/p', log_id='ses-s'), 3)
        self.assertEqual(self.unrevoked(), [('run-1', 'ses-s', '/p')])
        self.assertEqual(self.rows(), [('run-1', 'ses-s', '/p', 3, None)])
        self.assertIsNone(self.granted('run-1', '/p', log_id='ses-s'))
        self.assertEqual(self.revoked('run-1'), 4)
        self.assertIsNone(self.revoked('run-1'))
        self.assertEqual(self.unrevoked(), [])
        self.assertEqual(self.rows(), [('run-1', 'ses-s', '/p', 3, 4)])
        self.assertEqual([(event['type'], event['session_id']) for event in self.store.events],
                         [('workspace.grant', 'ses-s'), ('workspace.revoke', 'ses-s')] * 2)

    def test_another_folder_or_log_for_a_recorded_run_is_refused_before_anything_is_written(self) -> None:
        self.granted('run-1', '/p', log_id='ses-s')
        for path, log_id in (('/q', 'ses-s'), ('/p', 'ses-t'), ('/p', None)):
            with self.assertRaisesRegex(ValueError, 'grant_conflict'):
                self.granted('run-1', path, log_id=log_id)
        self.revoked('run-1')
        with self.assertRaisesRegex(ValueError, 'grant_conflict'):
            self.granted('run-1', '/q', log_id='ses-s')
        self.assertEqual([event['type'] for event in self.store.events], ['workspace.grant', 'workspace.revoke'])
        self.assertEqual(self.rows(), [('run-1', 'ses-s', '/p', 1, 2)])

    def test_a_run_with_no_session_is_logged_in_its_own_log_never_another_runs(self) -> None:
        with self.assertRaisesRegex(ValueError, 'log_id_invalid'):
            self.granted('run-a', '/p', log_id='run:run-b')
        with self.assertRaisesRegex(ValueError, 'log_id_invalid'):
            G.access_event('workspace.revoke', 'run-a', '/p', log_id='run:run-ab')
        self.assertEqual(self.store.events, [])
        self.assertEqual(self.granted('run-a', '/p', log_id='run:run-a'), 1)
        self.assertEqual(self.store.events[0]['session_id'], 'run:run-a')

    def test_a_revoke_is_asked_with_a_run_id_the_envelope_takes(self) -> None:
        for run_id in ('bad run', '', 'r' * 65, 5, None):
            with self.assertRaisesRegex(ValueError, 'run_id_invalid'):
                self.revoked(run_id)

    def test_the_table_keeps_one_row_per_run(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction() as tx:
                tx.execute('INSERT INTO workspace_grant VALUES (?, ?, ?, ?, ?)', ('run-1', 'run:run-1', '/p', 1, None))
                tx.execute('INSERT INTO workspace_grant VALUES (?, ?, ?, ?, ?)', ('run-1', 'run:run-1', '/p', 2, None))
        self.assertEqual(self.rows(), [])

    def test_a_revoke_is_appended_once_and_only_after_a_grant(self) -> None:
        self.assertIsNone(self.revoked('run-1'))
        self.granted('run-1', '/p')
        self.assertIsNotNone(self.revoked('run-1'))
        self.assertIsNone(self.revoked('run-1'))
        self.assertEqual([event['type'] for event in self.store.events], ['workspace.grant', 'workspace.revoke'])

    def test_a_run_in_a_session_is_logged_in_the_session(self) -> None:
        self.granted('run-2', '/p', log_id='ses-abc')
        self.revoked('run-2')
        self.assertEqual([event['session_id'] for event in self.store.events], ['ses-abc', 'ses-abc'])

    def test_the_runs_never_revoked_are_listed_for_a_startup(self) -> None:
        self.granted('run-b', '/b')
        self.granted('run-a', '/a', log_id='ses-x')
        self.granted('run-c', '/c')
        self.revoked('run-c')
        self.assertEqual(self.unrevoked(), [('run-a', 'ses-x', '/a'), ('run-b', 'run:run-b', '/b')])

    def test_a_grant_whose_transaction_rolls_back_leaves_no_row_and_no_event(self) -> None:
        with self.assertRaises(RuntimeError):
            with self.store.transaction() as tx:
                G.Ledger.granted(tx, 'run-1', '/p')
                raise RuntimeError('the caller failed after the grant')
        self.assertEqual(self.store.events, [])
        self.assertEqual(self.store.rows, {})
        self.assertIsNotNone(self.granted('run-1', '/p'))

    def test_ids_and_paths_that_break_the_envelope_are_refused_before_anything_is_written(self) -> None:
        for run_id, path, log_id in (('', '/p', None), ('run 1', '/p', None), ('r' * 65, '/p', None), (5, '/p', None),
                                     ('run-1\n', '/p', None), ('run-1', '', None), ('run-1', None, None),
                                     ('run-1', '/p', 'run-1'), ('run-1', '/p', 'ses-'), ('run-1', '/p', 5),
                                     ('run-1', '/p', 'ses-' + 'a' * 61), ('run-1', '/p', 'ses-x\n'),
                                     ('run-1', '/p', 'run:run-1\n'), ('run-1', '/p', 'ses-x y')):
            with self.assertRaises(ValueError):
                self.granted(run_id, path, log_id=log_id)
        with self.assertRaises(ValueError):
            G.access_event('workspace.other', 'run-1', '/p')
        self.assertEqual(self.store.events, [])
        self.assertEqual(self.store.rows, {})

    def test_the_longest_ids_the_envelope_takes_are_taken(self) -> None:
        self.assertEqual(G.access_event('workspace.grant', 'r' * 64, '/p')['session_id'], 'run:' + 'r' * 64)
        self.assertEqual(G.access_event('workspace.grant', 'run-1', '/p', log_id='ses-' + 'a' * 60)['session_id'],
                         'ses-' + 'a' * 60)

    def test_the_events_are_published_event_records(self) -> None:
        if fits is None:
            self.skipTest('lane SPINE\'s conformance reader and schemas are not in this tree')
        self.granted('run-1', '/home/dev/projects/atlas', log_id='ses-abc')
        self.revoked('run-1')
        for seq, event in enumerate(self.store.events, start=1):
            stored = dict(event, seq=seq, time='2026-11-14T23:40:00.000Z')
            self.assertTrue(fits('event.v1.json', stored), stored)
            self.assertTrue(fits(f'event.v1.json#/$defs/{event["type"]}', event['payload']))


if __name__ == '__main__':
    unittest.main()
