"""Regressions for the third reads of draft #386: two Codex reads (gpt-6.1-sol, gpt-6-astra) and a DeepSeek break-it
read. Each test names the finding it pins. Every key is made up; no real keychain, bus or terminal is reached."""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path

from recursion_core.keys import __main__ as entry
from recursion_core.keys import cli, validate
from recursion_core.keys.backends import OwnerFile
from recursion_core.keys.errors import KeyRefused, KeyStoreError
from recursion_core.keys.files import open_folder
from recursion_core.keys.store import UNBOUND, KeyStore
from recursion_core.keys.tests.support import (
    MARKER,
    OTHER,
    FakeSystem,
    TempStoreCase,
    attribute_pairs,
    keychain_system,
    needs_posix,
)


def fixed_clock() -> float:
    return 1_763_160_000.25


def run_main(store, *argv):
    out, err = io.StringIO(), io.StringIO()
    status = entry.main(list(argv), out=out, err=err, store=store)
    return status, out.getvalue() + err.getvalue()


def overwrite(path: Path, data: bytes) -> None:
    """Put older bytes back in place, as a restore would: same file, same mode."""
    with open(path, 'r+b') as fh:
        fh.truncate(0)
        fh.write(data)


@needs_posix
class ValueBindingTest(TempStoreCase):
    """Sol change 1 (P2), astra change 1 (P1), DeepSeek F1 (MEDIUM), and the Liaison's 10:20 PM ruling: on every
    backend a value is released only when its save id, provider and store id match the record being used."""

    def backends(self):
        """(label, store factory, fake keychain or None): the owner-only file and the Secret Service."""
        system, chain = keychain_system()
        yield 'owner_file', (lambda root: KeyStore(root, system=FakeSystem(), clock=fixed_clock)), None
        yield 'secret_service', (lambda root: KeyStore(root, system=system, clock=fixed_clock)), chain

    def test_witness_an_older_index_put_back_never_authorizes_a_later_unpublished_value(self) -> None:
        for label, make, _ in self.backends():
            with self.subTest(backend=label):
                store = make(self.base / label / 'keys')
                self.add(store)                                            # openai, with an opt-in
                older = (store.root / 'index.json').read_bytes()
                store.remove('codex-key')
                store.add('codex-key', 'mistral', 'CODEX_API_KEY', OTHER)  # unpublished, no opt-in
                overwrite(store.root / 'index.json', older)
                self.assertEqual(store.list().keys['codex-key'].provider, 'openai')
                with self.assertRaises(KeyRefused) as refused:
                    store.credential('codex-key', 'CODEX_API_KEY')
                self.assertEqual(refused.exception.code, 'secret_missing')

    def test_an_edited_provider_never_reaches_the_value(self) -> None:
        for label, make, _ in self.backends():
            with self.subTest(backend=label):
                store = make(self.base / label / 'keys')
                store.add('codex-key', 'mistral', 'CODEX_API_KEY', OTHER, metered_spend_allowed=True, cap_usd=5)
                path = store.root / 'index.json'
                doc = json.loads(path.read_text())
                doc['keys']['codex-key']['provider'] = 'openai'           # same save id, a published vendor
                overwrite(path, (json.dumps(doc, indent=1) + '\n').encode())
                with self.assertRaises(KeyRefused) as refused:
                    store.credential('codex-key', 'CODEX_API_KEY')
                self.assertEqual(refused.exception.code, 'secret_missing')

    def test_an_owner_file_index_copied_in_from_another_store_is_refused(self) -> None:
        first = KeyStore(self.base / 'first' / 'keys', system=FakeSystem(), clock=fixed_clock)
        second = KeyStore(self.base / 'second' / 'keys', system=FakeSystem(), clock=fixed_clock)
        self.add(first)
        self.add(second, value=OTHER)
        overwrite(first.root / 'index.json', (second.root / 'index.json').read_bytes())
        with self.assertRaises(KeyRefused) as refused:
            first.credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'secret_missing')     # the file names another store (round 4)

    def test_an_owner_file_value_without_its_binding_is_refused(self) -> None:
        store = KeyStore(self.root, system=FakeSystem(), clock=fixed_clock)
        self.add(store)
        good = json.loads((self.root / 'secrets.json').read_text())
        entry_ = good['secrets']['codex-key']
        for label, doc in (('a bare value', {'version': 1, 'secrets': {'codex-key': MARKER}}),
                           ('no store id', {'version': 1, 'secrets': good['secrets']}),
                           ('a bad store id', dict(good, store_id='z' * 32)),
                           ('no save id', dict(good, secrets={'codex-key': {'provider': 'openai', 'value': MARKER}})),
                           ('a bad save id', dict(good, secrets={'codex-key': dict(entry_, save_id='A' * 32)})),
                           ('no provider', dict(good, secrets={'codex-key': {'save_id': entry_['save_id'],
                                                                             'value': MARKER}})),
                           ('a bad provider', dict(good, secrets={'codex-key': dict(entry_, provider='Open AI')})),
                           ('an extra field', dict(good, secrets={'codex-key': dict(entry_, env='X')}))):
            with self.subTest(case=label):
                overwrite(self.root / 'secrets.json', json.dumps(doc).encode())
                with self.assertRaises(KeyRefused) as refused:
                    store.credential('codex-key', 'CODEX_API_KEY')
                self.assertEqual(refused.exception.code, 'owner_file_invalid')

    def test_a_malformed_store_id_is_refused_by_the_audit_too(self) -> None:
        store = KeyStore(self.root, system=FakeSystem(), clock=fixed_clock)
        self.add(store)
        good = json.loads((self.root / 'secrets.json').read_text())
        overwrite(self.root / 'secrets.json', json.dumps(dict(good, store_id='z' * 32)).encode())
        os.unlink(self.root / 'index.json')            # no record: only the audit's own read of the file is left
        with self.assertRaises(KeyStoreError) as caught:
            store.values()
        self.assertEqual(caught.exception.code, 'owner_file_invalid')

    def test_the_owner_file_releases_only_to_a_matching_record(self) -> None:
        store = KeyStore(self.root, system=FakeSystem(), clock=fixed_clock)
        record = self.add(store)
        store_id = store.list().store_id
        with open_folder(self.root, create=False) as folder:
            backend = OwnerFile(store_id)
            self.assertEqual(backend.get('codex-key', folder, record), MARKER)
            self.assertEqual(backend.get('codex-key', folder), MARKER)       # the audit's read, no release
            for changed in ({'save_id': '0' * 32}, {'provider': 'anthropic'}):
                with self.subTest(changed=changed):
                    other = record.__class__(**{**record.__dict__, **changed})
                    with self.assertRaises(KeyStoreError) as caught:
                        backend.get('codex-key', folder, other)
                    self.assertEqual(caught.exception.code, 'secret_missing')
            with self.assertRaises(KeyStoreError) as caught:
                OwnerFile('f' * 32).get('codex-key', folder, record)
            self.assertEqual(caught.exception.code, 'secret_missing')   # releases nothing to another store

    def test_a_keychain_item_is_bound_to_save_provider_and_store(self) -> None:
        system, chain = keychain_system()
        store = KeyStore(self.root, system=system, clock=fixed_clock)
        record = self.add(store)
        (key,) = chain.items
        self.assertEqual({k: v for k, v in attribute_pairs(key).items() if k.startswith('recursion_')},
                         {'recursion_store': store.list().store_id, 'recursion_key_id': 'codex-key',
                          'recursion_save': record.save_id, 'recursion_provider': 'openai'})
        for word, replacement in (('recursion_store', 'f' * 32), ('recursion_provider', 'anthropic')):
            with self.subTest(attribute=word):
                pairs = dict(attribute_pairs(key), **{word: replacement})
                chain.items = {tuple(w for pair in pairs.items() for w in pair): chain.items[key]}
                with self.assertRaises(KeyRefused) as refused:
                    store.credential('codex-key', 'CODEX_API_KEY')
                self.assertEqual(refused.exception.code, 'secret_missing')
                chain.items = {key: MARKER.encode()}
        self.assertEqual(store.credential('codex-key', 'CODEX_API_KEY'), MARKER)

    def test_a_removal_in_one_store_leaves_another_stores_value(self) -> None:
        system, chain = keychain_system()
        first = KeyStore(self.base / 'first' / 'keys', system=system, clock=fixed_clock)
        second = KeyStore(self.base / 'second' / 'keys', system=system, clock=fixed_clock)
        self.add(first)
        self.add(second, value=OTHER)
        first.remove('codex-key')
        self.assertEqual(second.credential('codex-key', 'CODEX_API_KEY'), OTHER)
        self.assertEqual(len(chain.items), 1)

    def test_two_records_naming_one_save_are_refused(self) -> None:
        store = KeyStore(self.root, system=FakeSystem(), clock=fixed_clock)
        self.add(store)
        self.add(store, key_id='second', value=OTHER)
        path = self.root / 'index.json'
        doc = json.loads(path.read_text())
        doc['keys']['second']['save_id'] = doc['keys']['codex-key']['save_id']
        overwrite(path, (json.dumps(doc, indent=1) + '\n').encode())
        with self.assertRaises(KeyStoreError) as caught:
            store.list()
        self.assertEqual(caught.exception.code, 'index_invalid')


@needs_posix
class UnboundValueAuditTest(TempStoreCase):
    """DeepSeek F2: a value in the owner-only file that no record reaches is searched for all the same."""

    def leak_system(self) -> FakeSystem:
        state = self.base / 'state'
        (state / 'recursion').mkdir(parents=True, exist_ok=True)
        return FakeSystem({'PATH': '/fake/bin', 'HOME': str(self.base), 'XDG_STATE_HOME': str(state)})

    def test_a_value_with_no_index_at_all_is_searched_for(self) -> None:
        store = KeyStore(self.root, system=FakeSystem(), clock=fixed_clock)
        self.add(store)
        os.unlink(self.root / 'index.json')
        system = self.leak_system()
        (self.base / 'state' / 'recursion' / 'run.log').write_text(MARKER)
        status, text = run_main(KeyStore(self.root, system=system), 'audit', '--no-journal')
        self.assertEqual(status, 1)
        self.assertIn(f'FOUND: the value of codex-key {UNBOUND} is in', text)
        self.assertNotIn(MARKER, text)

    def test_a_value_no_record_reaches_after_a_restore_is_searched_for(self) -> None:
        store = KeyStore(self.root, system=FakeSystem(), clock=fixed_clock)
        self.add(store)
        older = (self.root / 'index.json').read_bytes()
        store.remove('codex-key')
        store.add('codex-key', 'mistral', 'CODEX_API_KEY', OTHER)
        overwrite(self.root / 'index.json', older)
        system = self.leak_system()
        (self.base / 'state' / 'recursion' / 'run.log').write_text(OTHER)
        found, missing = KeyStore(self.root, system=system).values()
        self.assertEqual(missing, {'codex-key': 'secret_missing'})
        self.assertEqual(found, {f'codex-key {UNBOUND}': OTHER})
        status, text = run_main(KeyStore(self.root, system=system), 'audit', '--no-journal')
        self.assertEqual(status, 1)
        self.assertIn('not searched for codex-key: its value could not be read (secret_missing)', text)
        self.assertIn(f'FOUND: the value of codex-key {UNBOUND}', text)

    def test_a_bound_value_is_searched_for_once(self) -> None:
        store = KeyStore(self.root, system=FakeSystem(), clock=fixed_clock)
        self.add(store)
        self.assertEqual(store.values(), ({'codex-key': MARKER}, {}))


class CommandLineTest(TempStoreCase):
    """DeepSeek F3. (F4 is not applied: see the pull request text.)"""

    def test_f3_a_namespace_with_no_action_is_a_usage_error(self) -> None:
        err = io.StringIO()
        status = cli.run(argparse.Namespace(), io.StringIO(), err, store=KeyStore(self.root, system=FakeSystem()))
        self.assertEqual(status, 2)
        self.assertIn('usage: key add|list|remove|audit', err.getvalue())


class KeyShapedIdRoundThreeTest(TempStoreCase):
    """DeepSeek's round-3 INFO on N1: a long word mixing letters and digits is refused; what still passes is said."""

    def test_the_mixed_word_rule_and_its_boundary(self) -> None:
        cases = {
            'abcdefgh12345678': True, 'abcdefgh1234': True, 'abcdefgh123': False,
            'deadbeef1234cafe': True, 'login-20261114': False, 'claude-3-5-sonnet-20241022': False,
            'gpt4o-mini': False, 'abcdefabcdefabcd': False, 'qwertyuiopasdfgh': False,
        }
        for text, keyish in cases.items():
            with self.subTest(id=text):
                self.assertIs(validate.looks_like_a_key(text), keyish)
