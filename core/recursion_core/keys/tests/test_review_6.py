"""Regressions for the sixth reads of draft #386: the DeepSeek break-it read (F1 to F5, and G1's three guards no test
held). Each test names the finding it pins. Every key is made up; no real keychain, bus or terminal is reached."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

from recursion_core.keys import __main__ as entry
from recursion_core.keys.errors import KeyStoreError
from recursion_core.keys.store import FOREIGN, UNBOUND, Index, KeyStore, index_bytes
from recursion_core.keys.tests.support import (
    MARKER,
    OTHER,
    Completed,
    FakeSystem,
    TempStoreCase,
    keychain_system,
    needs_posix,
)

THIRD = 'madeup-KEYS-third-91cc-not-a-real-key'
FOURTH = 'madeup-KEYS-fourth-5e2a-not-a-real-key'
THEIRS = (json.dumps({'version': 1, 'store_id': 'f' * 32, 'secrets': {                  # another store's file
    'theirs': {'save_id': 'e' * 32, 'provider': 'openai', 'value': THIRD}}}, indent=1) + '\n').encode()
MALFORMED = b'a stray file nobody wrote here\n'


def fixed_clock() -> float:
    return 1_763_160_000.25


def run_main(store, *argv):
    out, err = io.StringIO(), io.StringIO()
    status = entry.main(list(argv), out=out, err=err, store=store)
    return status, out.getvalue() + err.getvalue()


def overwrite(path: Path, data: bytes) -> None:
    with open(path, 'r+b') as fh:
        fh.truncate(0)
        fh.write(data)


def write_new(path: Path, data: bytes, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, 'wb') as fh:
        fh.write(data)
    os.chmod(path, mode)


def secrets(root: Path) -> dict:
    return json.loads((root / 'secrets.json').read_text())['secrets']


def lose_the_index(root: Path, shape: str) -> None:
    """``lost``: no index at all; ``emptied``: every record gone, the store's id kept."""
    if shape == 'lost':
        os.unlink(root / 'index.json')
    else:
        store_id = json.loads((root / 'index.json').read_text())['store_id']
        overwrite(root / 'index.json', index_bytes(Index(None, {}, {}, store_id)))


def index_raw(root: Path):
    path = root / 'index.json'
    return path.read_bytes() if path.exists() else None


@needs_posix
class UnnamedValueOnAKeychainHostTest(TempStoreCase):
    """DeepSeek F1 (MEDIUM): with no record pinning the store, ``remove <id>`` reaches a value of the id that the
    store's own owner-only file holds whatever the host offers now, and takes no store id doing it."""

    def file_store_then_a_keychain(self, shape: str):
        root = self.base / shape / 'keys'
        on_file = KeyStore(root, system=FakeSystem(), clock=fixed_clock)
        self.add(on_file, key_id='alpha', value=MARKER)
        self.add(on_file, key_id='beta', value=OTHER, env='ANTHROPIC_API_KEY', provider='anthropic')
        lose_the_index(root, shape)
        system, chain = keychain_system()
        return KeyStore(root, system=system, clock=fixed_clock), system, chain

    def test_remove_reaches_the_files_value_when_a_keychain_answers(self) -> None:
        for shape in ('emptied', 'lost'):
            with self.subTest(index=shape):
                store, system, _ = self.file_store_then_a_keychain(shape)
                file_id = json.loads((store.root / 'secrets.json').read_text())['store_id']
                before = index_raw(store.root)
                self.assertIsNone(store.remove('alpha'))                         # no record named it
                self.assertEqual(sorted(secrets(store.root)), ['beta'])
                self.assertEqual(system.calls, [])                               # no host or keychain was asked
                self.assertEqual(index_raw(store.root), before)                  # nor the index changed: no id taken
                status, text = run_main(store, 'remove', 'beta', '--yes')
                self.assertEqual(status, 0)
                self.assertIn('removed a value of beta that no saved key named', text)
                self.assertEqual(secrets(store.root), {})
                status, text = run_main(store, 'audit', '--no-journal')
                self.assertEqual(status, 0)
                self.assertNotIn('not named', text)
                record = self.add(store, key_id='fresh', value=FOURTH)            # a keychain save takes no file id
                self.assertEqual(record.store, 'secret_service')
                self.assertEqual(store.list().store_id == file_id, shape == 'emptied')
                with self.assertRaises(KeyStoreError) as caught:
                    store.remove('nobody')
                self.assertEqual(caught.exception.code, 'key_unknown')

    def test_a_keychain_key_and_the_files_value_of_its_id_both_go(self) -> None:
        store, _, chain = self.file_store_then_a_keychain('emptied')
        self.add(store, key_id='alpha', value=FOURTH)                            # the keychain save reads no file
        self.assertEqual(sorted(secrets(store.root)), ['alpha', 'beta'])
        status, text = run_main(store, 'audit', '--no-journal')
        self.assertEqual(status, 0)
        self.assertIn("not this store's: ", text)                                # its keys are in a keychain now
        self.assertNotIn('"key remove alpha" deletes it', text)
        self.assertEqual(store.remove('alpha').store, 'secret_service')          # the keychain's key first
        self.assertEqual(chain.items, {})
        self.assertIsNone(store.remove('alpha'))                                 # then the file's value
        self.assertEqual(sorted(secrets(store.root)), ['beta'])
        status, text = run_main(store, 'audit', '--no-journal')
        self.assertIn('not named: the owner-only file holds a value of beta that no saved key names; '
                      '"key remove beta" deletes it', text)
        for value in (MARKER, OTHER, FOURTH):
            self.assertNotIn(value, text)


@needs_posix
class FileAKeychainStoreDoesNotUseTest(TempStoreCase):
    """DeepSeek F2 (LOW) and G1 (store.py:294): a file beside a store whose keys are in a keychain never refuses
    its audit or its removals; one it cannot read is named unreadable and makes the audit incomplete."""

    def keychain_store(self, name: str):
        system, chain = keychain_system()
        store = KeyStore(self.base / name / 'keys', system=system, clock=fixed_clock)
        self.add(store, key_id='alpha', value=MARKER)
        return store, chain

    def test_a_file_it_cannot_read_is_named_unreadable(self) -> None:
        for kind in ('malformed', 'mode 0644', 'a folder'):
            with self.subTest(file=kind):
                store, _ = self.keychain_store(kind.replace(' ', '-'))
                path = store.root / 'secrets.json'
                if kind == 'a folder':
                    path.mkdir()
                else:
                    write_new(path, MALFORMED if kind == 'malformed' else THEIRS, 0o600 if kind == 'malformed'
                              else 0o644)
                unreadable = []
                found, missing = store.values(unreadable=unreadable)
                self.assertEqual((found, missing, unreadable), ({'alpha': MARKER}, {}, [str(path)]))
                status, text = run_main(store, 'audit', '--no-journal')
                self.assertEqual(status, 4)                                      # incomplete, not refused
                self.assertIn(f'unreadable: {path}', text)
                leak = store.root.parent / 'leak.txt'
                leak.write_text(MARKER)
                status, text = run_main(store, 'audit', '--no-journal')
                self.assertEqual(status, 1)                                      # and it still searches
                self.assertIn(f'FOUND: the value of alpha is in {leak}', text)
                self.assertNotIn(MARKER, text)
                with self.assertRaises(KeyStoreError) as caught:                 # a direct caller is still refused
                    store.values()
                self.assertIn(caught.exception.code, ('owner_file_invalid', 'file_invalid'))

    def test_a_file_it_can_read_is_searched_and_said_not_to_be_its_own(self) -> None:
        store, _ = self.keychain_store('theirs')
        write_new(store.root / 'secrets.json', THEIRS)
        self.assertEqual(store.values(), ({'alpha': MARKER, f'theirs {FOREIGN}': THIRD}, {}))
        (store.root.parent / 'leak.txt').write_text(THIRD)
        status, text = run_main(store, 'audit', '--no-journal')
        self.assertEqual(status, 1)
        self.assertIn(f'FOUND: a value of theirs that no saved key names is in {store.root.parent / "leak.txt"}',
                      text)
        self.assertIn(f"not this store's: {store.root / 'secrets.json'} holds a value of theirs", text)
        self.assertNotIn(THIRD, text)

    def test_a_removal_reads_no_file_beside_a_keychain_store(self) -> None:
        for kind, data in (('malformed', MALFORMED), ('another store', THEIRS)):
            with self.subTest(file=kind):
                store, _ = self.keychain_store(f'remove-{kind[0]}')
                write_new(store.root / 'secrets.json', data)
                for key_id in ('nobody', 'theirs'):
                    with self.assertRaises(KeyStoreError) as caught:
                        store.remove(key_id)
                    self.assertEqual(caught.exception.code, 'key_unknown')
                self.assertEqual((store.root / 'secrets.json').read_bytes(), data)

    def test_the_stores_own_file_that_it_cannot_read_still_refuses_the_audit(self) -> None:
        store = KeyStore(self.root, system=FakeSystem(), clock=fixed_clock)
        self.add(store)
        overwrite(self.root / 'secrets.json', MALFORMED)
        status, text = run_main(store, 'audit', '--no-journal')
        self.assertEqual(status, 3)
        self.assertIn('refused: owner_file_invalid', text)


@needs_posix
class AnotherStoresFileTest(TempStoreCase):
    """G1 (backends.py:211-214): a file naming another store, beside an index with records on the owner-only file,
    holds nothing this store removes; a removal of an id only it holds is ``key_unknown`` and changes nothing."""

    def test_remove_of_an_id_only_another_stores_file_holds(self) -> None:
        store = KeyStore(self.root, system=FakeSystem(), clock=fixed_clock)
        self.add(store, key_id='beta', value=MARKER)
        os.unlink(self.root / 'secrets.json')
        write_new(self.root / 'secrets.json', THEIRS)
        with self.assertRaises(KeyStoreError) as caught:
            store.remove('theirs')
        self.assertEqual(caught.exception.code, 'key_unknown')
        status, text = run_main(store, 'remove', 'theirs', '--yes')
        self.assertEqual(status, 1)
        self.assertIn('refused: key_unknown', text)
        self.assertEqual((self.root / 'secrets.json').read_bytes(), THEIRS)
        status, text = run_main(store, 'audit', '--no-journal')
        self.assertIn(f"not this store's: {self.root / 'secrets.json'} holds a value of theirs", text)
        self.assertNotIn('"key remove theirs"', text)


@needs_posix
class AuditCountsTest(TempStoreCase):
    """DeepSeek F3 (LOW): the audit's summary counts saved keys from the index, and the values it looked for
    beside them."""

    def test_saved_keys_and_values_are_counted_apart(self) -> None:
        for shape, keys in (('lost', 0), ('emptied', 0), ('one left', 1)):
            with self.subTest(index=shape):
                store = KeyStore(self.base / shape.replace(' ', '-') / 'keys', system=FakeSystem(), clock=fixed_clock)
                self.add(store, key_id='alpha', value=MARKER)
                older = (store.root / 'index.json').read_bytes()
                self.add(store, key_id='beta', value=OTHER, env='ANTHROPIC_API_KEY', provider='anthropic')
                self.add(store, key_id='gamma', value=THIRD, env='GEMINI_API_KEY', provider='google')
                if shape == 'one left':
                    overwrite(store.root / 'index.json', older)
                else:
                    lose_the_index(store.root, shape)
                self.assertEqual(len(store.list().keys), keys)
                status, text = run_main(store, 'audit', '--no-journal')
                self.assertEqual(status, 0)
                self.assertIn(f'audit: {keys} saved key(s), 3 value(s) looked for; ', text)


@needs_posix
class KeychainItemsBySaveTest(TempStoreCase):
    """DeepSeek F4 (LOW): an item is named by its record's save, so an item of a named id that another save stored
    is counted; an unfinished operation names every item of its id."""

    def test_an_item_of_a_named_id_and_another_save_is_counted(self) -> None:
        system, chain = keychain_system()
        store = KeyStore(self.root, system=system, clock=fixed_clock)
        self.add(store, key_id='alpha', value=MARKER)
        older = (self.root / 'index.json').read_bytes()
        store.remove('alpha')
        self.add(store, key_id='alpha', value=OTHER)
        overwrite(self.root / 'index.json', older)                               # names alpha's first save
        self.assertEqual(len(chain.items), 1)
        self.assertEqual(store.unnamed_items(), 1)
        status, text = run_main(store, 'audit', '--no-journal')
        self.assertEqual(status, 4)
        self.assertIn('keychain: 1 item(s) in the Secret Service that Recursion saved and no saved key of this store '
                      'names', text)
        self.assertIn('"key remove <id>" deletes every item of this store under an id a saved key has', text)
        self.assertNotIn(OTHER, text)
        self.assertEqual(store.remove('alpha').key_id, 'alpha')                  # every save of the id goes
        self.assertEqual(chain.items, {})
        self.assertEqual(store.unnamed_items(), 0)

    def test_an_unfinished_add_names_every_item_of_its_id(self) -> None:
        system, chain = keychain_system()
        store = KeyStore(self.root, system=system, clock=fixed_clock)
        self.add(store, key_id='alpha', value=MARKER)
        chain.after_store = Completed(1, b'', b'secret-tool: The name org.freedesktop.secrets was not provided\n')
        with self.assertRaises(KeyStoreError):
            self.add(store, key_id='beta', value=OTHER, env='ANTHROPIC_API_KEY', provider='anthropic')
        self.assertEqual(sorted(store.list().pending), ['beta'])
        self.assertEqual(len(chain.items), 2)
        self.assertEqual(store.unnamed_items(), 0)


@needs_posix
class FoundLineTest(TempStoreCase):
    """DeepSeek F5 (LOW): a finding names a bare key id, whatever reaches the value."""

    def test_a_finding_for_a_value_no_record_names_names_its_id(self) -> None:
        store = KeyStore(self.root, system=FakeSystem(), clock=fixed_clock)
        self.add(store, key_id='alpha', value=MARKER)
        older = (self.root / 'index.json').read_bytes()
        self.add(store, key_id='beta', value=OTHER, env='ANTHROPIC_API_KEY', provider='anthropic')
        overwrite(self.root / 'index.json', older)                               # names alpha, not beta
        leak = self.root.parent / 'leak.txt'
        leak.write_text(MARKER + '\n' + OTHER)
        status, text = run_main(store, 'audit', '--no-journal')
        self.assertEqual(status, 1)
        self.assertIn(f'FOUND: the value of alpha is in {leak}', text)
        self.assertIn(f'FOUND: a value of beta that no saved key names is in {leak}', text)
        self.assertNotIn(UNBOUND, text)
        self.assertNotIn(FOREIGN, text)
        for value in (MARKER, OTHER):
            self.assertNotIn(value, text)


@needs_posix
class UnfinishedAddWithAFailingHelperTest(TempStoreCase):
    """G1 (store.py:467-469): a value an unfinished add may have left, on a keychain that fails now, is no saved
    key's and refuses nothing; the audit says the add is unfinished."""

    def test_values_does_not_refuse(self) -> None:
        system, chain = keychain_system()
        store = KeyStore(self.root, system=system, clock=fixed_clock)
        self.add(store, key_id='alpha', value=MARKER)
        chain.after_store = Completed(1, b'', b'secret-tool: The name org.freedesktop.secrets was not provided\n')
        with self.assertRaises(KeyStoreError):
            self.add(store, key_id='beta', value=OTHER, env='ANTHROPIC_API_KEY', provider='anthropic')
        chain.mode = 'fail'
        self.assertEqual(store.values(), ({}, {'alpha': 'keychain_failed'}))
        status, text = run_main(store, 'audit', '--no-journal')
        self.assertEqual(status, 4)
        self.assertIn('unfinished add of beta', text)
        self.assertIn('not searched for alpha: its value could not be read (keychain_failed)', text)
