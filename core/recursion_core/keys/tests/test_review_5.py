"""Regressions for the fifth reads of draft #386: two Codex reads (gpt-6-astra, gpt-6.1-sol: merge as is) and a
DeepSeek break-it read (H1, M2, M3, L1, I1). Each test names the finding it pins. Every key is made up; no real
keychain, bus or terminal is reached."""
from __future__ import annotations

import io
import json
import os
import shutil
from pathlib import Path

from recursion_core.keys import __main__ as entry
from recursion_core.keys.errors import KeyRefused, KeyStoreError
from recursion_core.keys.store import UNBOUND, Index, KeyStore, index_bytes
from recursion_core.keys.tests.support import (
    MARKER,
    OTHER,
    FakeSystem,
    TempStoreCase,
    keychain_system,
    needs_posix,
)

THIRD = 'madeup-KEYS-third-91cc-not-a-real-key'
FOURTH = 'madeup-KEYS-fourth-5e2a-not-a-real-key'
SHAPES = ('lost', 'emptied', 'older')       # how an index stops naming a value the store holds


def fixed_clock() -> float:
    return 1_763_160_000.25


def run_main(store, *argv):
    out, err = io.StringIO(), io.StringIO()
    status = entry.main(list(argv), out=out, err=err, store=store)
    return status, out.getvalue() + err.getvalue()


def overwrite(path: Path, data: bytes) -> None:
    """Replace a file's bytes in place, keeping its mode and owner."""
    with open(path, 'r+b') as fh:
        fh.truncate(0)
        fh.write(data)


def write_new(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as fh:
        fh.write(data)


def secrets(root: Path) -> dict:
    return json.loads((root / 'secrets.json').read_text())['secrets']


def empty_the_index(root: Path) -> None:
    """Every record and intent gone, the store's id kept: what ``index_bytes`` writes for an empty store."""
    store_id = json.loads((root / 'index.json').read_text())['store_id']
    overwrite(root / 'index.json', index_bytes(Index(None, {}, {}, store_id)))


@needs_posix
class ValuesNoRecordNamesTest(TempStoreCase):
    """DeepSeek H1 (HIGH): a save, a removal or a recovery after the index was lost, emptied or put back deletes no
    value of another id; the audit keeps searching for those values and names them; ``remove <id>`` deletes one;
    and no save replaces one."""

    def three(self, name: str):
        """A store on the owner-only file with alpha, beta and gamma, and the index as it was after alpha alone."""
        store = KeyStore(self.base / name / 'keys', system=FakeSystem(), clock=fixed_clock)
        self.add(store, key_id='alpha', value=MARKER)
        older = (store.root / 'index.json').read_bytes()
        self.add(store, key_id='beta', value=OTHER, env='ANTHROPIC_API_KEY', provider='anthropic')
        self.add(store, key_id='gamma', value=THIRD, env='GEMINI_API_KEY', provider='google')
        return store, older, secrets(store.root)

    @staticmethod
    def lose(store: KeyStore, shape: str, older: bytes) -> None:
        if shape == 'lost':
            os.unlink(store.root / 'index.json')
        elif shape == 'emptied':
            empty_the_index(store.root)
        else:
            overwrite(store.root / 'index.json', older)      # an older index put back: it names alpha only

    def test_a_save_keeps_every_value_the_index_no_longer_names(self) -> None:
        for shape in SHAPES:
            with self.subTest(shape=shape):
                store, older, before = self.three(f'save-{shape}')
                self.lose(store, shape, older)
                record = self.add(store, key_id='fresh', value=FOURTH, env='XAI_API_KEY', provider='xai')
                after = secrets(store.root)
                self.assertEqual({kid: entry for kid, entry in after.items() if kid != 'fresh'}, before)
                self.assertEqual(after['fresh'], {'save_id': record.save_id, 'provider': 'xai', 'value': FOURTH})
                self.assertEqual(store.credential('fresh', 'XAI_API_KEY'), FOURTH)
                expected = {'fresh': FOURTH}
                for kid, entry in before.items():
                    named = shape == 'older' and kid == 'alpha'
                    expected[kid if named else f'{kid} {UNBOUND}'] = entry['value']
                self.assertEqual(store.values(), (expected, {}))
                status, text = run_main(store, 'audit', '--no-journal')
                self.assertEqual(status, 0)
                self.assertIn('audit: 4 saved key(s)', text)                     # the count no longer drops
                for kid in ('beta', 'gamma'):
                    self.assertIn(f'not named: the owner-only file holds a value of {kid} that no saved key names',
                                  text)
                self.assertEqual('value of alpha that no saved key names' in text, shape != 'older')
                for value in (MARKER, OTHER, THIRD, FOURTH):
                    self.assertNotIn(value, text)

    def test_a_removal_or_a_recovery_deletes_only_the_id_it_names(self) -> None:
        store, older, before = self.three('remove')
        self.lose(store, 'older', older)
        self.assertEqual(store.remove('alpha').key_id, 'alpha')
        self.assertEqual(secrets(store.root), {kid: entry for kid, entry in before.items() if kid != 'alpha'})
        path = store.root / 'index.json'
        doc = json.loads(path.read_text())
        doc['backend'] = 'owner_file'
        doc['pending'] = {'ghost': {'op': 'add', 'started_at': '2025-11-14T22:40:00.250Z'}}
        overwrite(path, (json.dumps(doc, indent=1) + '\n').encode())
        self.add(store, key_id='fresh', value=FOURTH, env='XAI_API_KEY', provider='xai')    # recovers the ghost first
        self.assertEqual(store.list().pending, {})
        self.assertEqual(sorted(secrets(store.root)), ['beta', 'fresh', 'gamma'])

    def test_remove_deletes_a_value_no_record_names(self) -> None:
        for shape in SHAPES:
            with self.subTest(shape=shape):
                store, older, _ = self.three(f'unnamed-{shape}')
                self.lose(store, shape, older)
                self.assertIsNone(store.remove('beta'))                          # no record named it
                self.assertEqual(sorted(secrets(store.root)), ['alpha', 'gamma'])
                index = store.list()
                self.assertEqual(index.pending, {})
                self.assertEqual(index.store_id, json.loads((store.root / 'secrets.json').read_text())['store_id'])
                status, text = run_main(store, 'remove', 'gamma', '--yes')
                self.assertEqual(status, 0)
                self.assertIn('removed a value of gamma that no saved key named', text)
                self.assertEqual(sorted(secrets(store.root)), ['alpha'])
                with self.assertRaises(KeyStoreError) as caught:
                    store.remove('nobody')
                self.assertEqual(caught.exception.code, 'key_unknown')

    def test_an_add_never_replaces_a_value_no_record_names(self) -> None:
        for shape in SHAPES:
            with self.subTest(shape=shape):
                store, older, before = self.three(f'replace-{shape}')
                self.lose(store, shape, older)
                raw = (store.root / 'secrets.json').read_bytes()
                with self.assertRaises(KeyStoreError) as caught:
                    self.add(store, key_id='gamma', value=FOURTH, env='GEMINI_API_KEY', provider='google')
                self.assertEqual(caught.exception.code, 'key_exists')
                self.assertNotIn(FOURTH, str(caught.exception))
                self.assertNotIn(THIRD, str(caught.exception))
                self.assertEqual((store.root / 'secrets.json').read_bytes(), raw)     # nothing written
                self.assertEqual(store.list().pending, {})                           # and no intent left
                self.assertIsNone(store.remove('gamma'))
                self.add(store, key_id='gamma', value=FOURTH, env='GEMINI_API_KEY', provider='google')
                self.assertEqual(store.credential('gamma', 'GEMINI_API_KEY'), FOURTH)
                self.assertEqual(secrets(store.root)['beta'], before['beta'])

    def test_a_keychain_store_asks_nothing_for_an_id_no_record_names(self) -> None:
        system, _ = keychain_system()
        store = KeyStore(self.root, system=system, clock=fixed_clock)
        self.add(store)
        system.calls.clear()
        with self.assertRaises(KeyStoreError) as caught:
            store.remove('nobody')
        self.assertEqual(caught.exception.code, 'key_unknown')
        self.assertEqual(system.calls, [])


STRAY = (       # an owner-only file in a keychain store's folder: one nobody wrote, and one of another store
    ('malformed', b'a stray file nobody wrote here\n'),
    ('another store', (json.dumps({'version': 1, 'store_id': 'f' * 32, 'secrets': {
        'theirs': {'save_id': 'e' * 32, 'provider': 'openai', 'value': THIRD}}}, indent=1) + '\n').encode()),
)


@needs_posix
class KeychainStoreOwnerFileTest(TempStoreCase):
    """DeepSeek M2 (MEDIUM) and I1 (INFO): a store on a keychain never takes the owner-only file's id and never needs
    the file to save, remove or release a key."""

    def test_a_stray_owner_file_never_stops_or_names_a_keychain_store(self) -> None:
        for shape in ('lost', 'emptied'):
            for kind, data in STRAY:
                with self.subTest(index=shape, file=kind):
                    system, _ = keychain_system()
                    store = KeyStore(self.base / f'{shape}-{kind[0]}' / 'keys', system=system, clock=fixed_clock)
                    self.add(store, key_id='alpha')
                    first_id = store.list().store_id
                    if shape == 'lost':
                        os.unlink(store.root / 'index.json')
                    else:
                        empty_the_index(store.root)
                    write_new(store.root / 'secrets.json', data)
                    record = self.add(store, key_id='fresh', value=OTHER)
                    self.assertEqual(record.store, 'secret_service')
                    store_id = store.list().store_id
                    self.assertNotEqual(store_id, 'f' * 32)                      # the file's id is never taken
                    self.assertEqual(store_id == first_id, shape == 'emptied')
                    self.assertEqual(store.credential('fresh', 'CODEX_API_KEY'), OTHER)
                    self.assertEqual(store.remove('fresh').key_id, 'fresh')
                    self.assertEqual((store.root / 'secrets.json').read_bytes(), data)   # never written


@needs_posix
class KeychainItemsNoRecordNamesTest(TempStoreCase):
    """DeepSeek M3 (MEDIUM): an index lost on a keychain store leaves its items with no name the store knows; the
    audit counts every Recursion item in the keychain no record names, and is incomplete while there is one."""

    def test_the_audit_counts_an_item_an_index_lost_or_emptied_leaves_unnamed(self) -> None:
        for shape in ('lost', 'emptied'):
            with self.subTest(index=shape):
                system, chain = keychain_system()
                store = KeyStore(self.base / shape / 'keys', system=system, clock=fixed_clock)
                self.add(store, key_id='alpha')
                self.assertEqual(store.unnamed_items(), 0)
                status, text = run_main(store, 'audit', '--no-journal')
                self.assertEqual(status, 0)
                self.assertNotIn('keychain:', text)
                if shape == 'lost':
                    os.unlink(store.root / 'index.json')
                else:
                    empty_the_index(store.root)
                self.assertEqual(store.unnamed_items(), 1)
                status, text = run_main(store, 'audit', '--no-journal')
                self.assertEqual(status, 4)
                self.assertIn('keychain: 1 item(s) in the Secret Service that Recursion saved and no saved key of '
                              'this store names', text)
                self.assertNotIn(MARKER, text)
                with self.assertRaises(KeyStoreError) as caught:                 # nothing names it here
                    store.remove('alpha')
                self.assertEqual(caught.exception.code, 'key_unknown')
                self.add(store, key_id='fresh', value=OTHER)
                self.assertEqual(len(chain.items), 2)
                self.assertEqual(store.unnamed_items(), 1)                       # the new save is named; the old not
                self.assertEqual(run_main(store, 'audit', '--no-journal')[0], 4)

    def test_another_stores_items_are_counted_too(self) -> None:
        system, _ = keychain_system()
        mine = KeyStore(self.base / 'mine' / 'keys', system=system, clock=fixed_clock)
        theirs = KeyStore(self.base / 'theirs' / 'keys', system=system, clock=fixed_clock)
        self.add(mine, key_id='alpha')
        self.add(theirs, key_id='alpha', value=OTHER)
        self.assertEqual(mine.unnamed_items(), 1)
        self.assertEqual(theirs.unnamed_items(), 1)
        theirs.remove('alpha')
        self.assertEqual(mine.unnamed_items(), 0)

    def test_items_that_cannot_be_counted_make_the_audit_incomplete(self) -> None:
        system, chain = keychain_system()
        store = KeyStore(self.root, system=system, clock=fixed_clock)
        self.add(store)
        chain.searched = 'fail'                                              # search fails; lookup still answers
        status, text = run_main(store, 'audit', '--no-journal')
        self.assertEqual(status, 4)
        self.assertIn('keychain: its items were not counted (keychain_failed)', text)

    def test_an_owner_file_store_counts_nothing(self) -> None:
        store = KeyStore(self.root, system=FakeSystem(), clock=fixed_clock)
        self.assertIsNone(store.unnamed_items())                         # no folder yet: the host offers the file
        self.add(store)
        self.assertIsNone(store.unnamed_items())
        status, text = run_main(store, 'audit', '--no-journal')
        self.assertEqual(status, 0)
        self.assertNotIn('keychain:', text)
        os.unlink(self.root / 'index.json')                              # no record and no pin: the host decides
        self.assertIsNone(store.unnamed_items())
        status, text = run_main(store, 'audit', '--no-journal')
        self.assertEqual(status, 0)
        self.assertNotIn('keychain:', text)


@needs_posix
class FolderCopyAndRemovalTextTest(TempStoreCase):
    """DeepSeek L1 (LOW) and its sentences 8.3: what store.py and the README now say, held by the code."""

    def test_a_copy_of_the_whole_folder_on_each_backend(self) -> None:
        for kind in ('owner_file', 'secret_service'):
            with self.subTest(backend=kind):
                system = keychain_system()[0] if kind == 'secret_service' else FakeSystem()
                original = KeyStore(self.base / kind / 'a' / 'keys', system=system, clock=fixed_clock)
                self.add(original, key_id='alpha')
                copy_root = self.base / kind / 'b' / 'keys'
                shutil.copytree(original.root, copy_root)
                copy_root.chmod(0o700)
                copy = KeyStore(copy_root, system=system, clock=fixed_clock)
                self.assertEqual(copy.list().store_id, original.list().store_id)      # one id
                self.assertEqual(copy.credential('alpha', 'CODEX_API_KEY'), MARKER)
                copy.remove('alpha')
                if kind == 'owner_file':      # a file of its own: the original keeps its value
                    self.assertEqual(original.credential('alpha', 'CODEX_API_KEY'), MARKER)
                else:                         # one set of items: the original's record now finds none
                    with self.assertRaises(KeyRefused) as refused:
                        original.credential('alpha', 'CODEX_API_KEY')
                    self.assertEqual(refused.exception.code, 'secret_missing')

    def test_a_removal_refuses_a_folder_in_place_of_the_owner_file(self) -> None:
        store = KeyStore(self.root, system=FakeSystem(), clock=fixed_clock)
        self.add(store)
        os.unlink(self.root / 'secrets.json')
        (self.root / 'secrets.json').mkdir()
        with self.assertRaises(KeyStoreError) as caught:
            store.remove('codex-key')
        self.assertEqual(caught.exception.code, 'file_invalid')
        self.assertIn('codex-key', store.list().keys)                         # the key stays on record
