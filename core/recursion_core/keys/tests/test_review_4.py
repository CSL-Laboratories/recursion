"""Regressions for the fourth reads of draft #386: two Codex reads (gpt-6.1-sol, gpt-6-astra) and a DeepSeek break-it
read. Each test names the finding it pins. Every key is made up; no real keychain, bus or terminal is reached."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

from recursion_core.keys import __main__ as entry
from recursion_core.keys.backends import OwnerFile
from recursion_core.keys.errors import KeyRefused, KeyStoreError
from recursion_core.keys.files import open_folder
from recursion_core.keys.store import UNBOUND, KeyStore
from recursion_core.keys.tests.support import MARKER, OTHER, FakeSystem, TempStoreCase, needs_posix


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


def file_store_id(root: Path) -> str:
    return json.loads((root / 'secrets.json').read_text())['store_id']


@needs_posix
class SaveIdShapeTest(TempStoreCase):
    """Astra 1, sol 1 (P2): a save id of the wrong type is index_invalid, not a TypeError."""

    def test_an_array_or_object_save_id_is_index_invalid(self) -> None:
        store = KeyStore(self.root, system=FakeSystem(), clock=fixed_clock)
        self.add(store)
        path = self.root / 'index.json'
        good = json.loads(path.read_text())
        for bad in ([], ['a' * 32], {}, {'id': 'a' * 32}, None, 7):
            with self.subTest(save_id=bad):
                doc = json.loads(json.dumps(good))
                doc['keys']['codex-key']['save_id'] = bad
                overwrite(path, (json.dumps(doc, indent=1) + '\n').encode())
                with self.assertRaises(KeyStoreError) as caught:
                    store.list()
                self.assertEqual(caught.exception.code, 'index_invalid')
                status, text = run_main(store, 'list')
                self.assertEqual(status, 1)
                self.assertIn('refused: index_invalid', text)
                self.assertNotIn('Traceback', text)


@needs_posix
class LostIndexRecoveryTest(TempStoreCase):
    """DeepSeek M1 (MEDIUM): a lost or replaced index.json leaves a store its own commands can recover."""

    def owner_store(self, root=None) -> KeyStore:
        return KeyStore(root or self.root, system=FakeSystem(), clock=fixed_clock)

    def test_a_lost_index_is_recovered_by_the_next_save(self) -> None:
        store = self.owner_store()
        self.add(store)
        old_id = file_store_id(self.root)
        kept = json.loads((self.root / 'secrets.json').read_text())['secrets']
        os.unlink(self.root / 'index.json')
        found, missing = store.values()
        self.assertEqual((found, missing), ({f'codex-key {UNBOUND}': MARKER}, {}))
        record = self.add(store, key_id='fresh', value=OTHER)
        self.assertEqual(store.list().store_id, old_id)                  # the folder's own store id, adopted
        self.assertEqual(store.credential('fresh', 'CODEX_API_KEY'), OTHER)
        self.assertEqual(json.loads((self.root / 'secrets.json').read_text())['secrets'],   # the earlier value
                         {**kept, 'fresh': {'save_id': record.save_id, 'provider': 'openai',     # stays (r5 H1)
                                            'value': OTHER}})
        self.assertEqual(store.list().pending, {})

    def test_a_replaced_index_can_be_listed_audited_and_cleared(self) -> None:
        store = self.owner_store()
        self.add(store)
        other = self.owner_store(self.base / 'other' / 'keys')
        self.add(other, key_id='theirs', value=OTHER)
        overwrite(self.root / 'index.json', (other.root / 'index.json').read_bytes())
        with self.assertRaises(KeyRefused) as refused:                  # releases nothing
            store.credential('theirs', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'secret_missing')
        status, text = run_main(store, 'audit', '--no-journal')        # searches, not refused
        self.assertEqual(status, 4)
        self.assertIn('not searched for theirs: its value could not be read (secret_missing)', text)
        with self.assertRaises(KeyStoreError) as caught:                 # no save over another store's file
            self.add(store, key_id='fresh', value=OTHER)
        self.assertEqual(caught.exception.code, 'owner_file_invalid')
        self.assertEqual(store.list().pending, {})                       # and no intent left behind
        self.assertEqual(store.remove('theirs').key_id, 'theirs')        # the record can always be removed
        self.add(store, key_id='fresh', value=OTHER)                      # an empty store adopts the file's id
        self.assertEqual(store.credential('fresh', 'CODEX_API_KEY'), OTHER)
        self.assertEqual(sorted(json.loads((self.root / 'secrets.json').read_text())['secrets']),
                         ['codex-key', 'fresh'])                       # this store's earlier value stays (r5 H1)

    def test_an_unfinished_operation_over_another_stores_file_is_cleared(self) -> None:
        store = self.owner_store()
        self.add(store)
        other = self.owner_store(self.base / 'other' / 'keys')
        self.add(other, key_id='theirs', value=OTHER)
        doc = json.loads((other.root / 'index.json').read_text())
        doc['pending'] = {'orphan': {'op': 'add', 'started_at': '2025-11-14T22:40:00.250Z'}}
        before = (self.root / 'secrets.json').read_bytes()
        overwrite(self.root / 'index.json', (json.dumps(doc, indent=1) + '\n').encode())
        self.assertIsNone(store.remove('orphan'))
        self.assertEqual(store.list().pending, {})
        self.assertEqual((self.root / 'secrets.json').read_bytes(), before)   # the other store's file untouched

    def test_the_file_of_another_store_holds_nothing_of_this_one(self) -> None:
        store = self.owner_store()
        record = self.add(store)
        with open_folder(self.root, create=False) as folder:
            foreign = OwnerFile('f' * 32)
            self.assertIs(foreign.holds('codex-key', folder), False)
            foreign.delete('codex-key', folder)                          # touches nothing
            self.assertEqual(OwnerFile(store.list().store_id).get('codex-key', folder, record), MARKER)
            self.assertEqual(foreign.get('codex-key', folder), MARKER)   # the audit's unbound read
            self.assertEqual(foreign.values(folder), {'codex-key': MARKER})
