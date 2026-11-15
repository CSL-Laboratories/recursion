"""The macOS Keychain backend, against a stand-in ``security`` (no Mac here, so it has not run on one).

The stand-in follows ``security help``: ``-i`` reads ``add-generic-password`` lines from standard input (``-a``
account, ``-s`` service, ``-G`` generic attribute, ``-j`` comment, ``-l`` label, ``-X`` the password as
hexadecimal; without ``-U`` an item of the same account and service is not replaced); ``find-generic-password``
and ``delete-generic-password`` match ``-a``, ``-s``, ``-G`` and ``-j`` and act on the first match; ``-w`` prints
the password and a newline; exit 44 is "no such item"; ``dump-keychain`` (never given ``-d``) prints each item's
attributes in its documented form and no password, beside three items of other programs. Every key is made up.
"""
from __future__ import annotations

import io
import json
import shlex

from recursion_core.keys import __main__ as entry
from recursion_core.keys import audit as audit_mod
from recursion_core.keys.errors import KeyRefused, KeyStoreError
from recursion_core.keys.store import Index, KeyStore, index_bytes
from recursion_core.keys.system import Completed
from recursion_core.keys.tests.support import (
    MARKER,
    OTHER,
    FakeSystem,
    TempStoreCase,
    needs_posix,
)

MATCH = {'-a': 'acct', '-s': 'svce', '-G': 'gena', '-j': 'icmt'}


class FakeSecurity:
    def __init__(self) -> None:
        self.items: list = []       # each {'acct', 'svce', 'gena', 'icmt', 'labl', 'password'}
        self.mode = 'ok'            # ok | locked | fails | drops | mangles | stuck | garbled | bare | hexform
        self.dumps: list = []       # every dump-keychain command line

    @staticmethod
    def _options(words) -> dict:
        return {words[i]: words[i + 1] for i in range(len(words) - 1) if words[i].startswith('-')}

    def _matches(self, options) -> list:
        wanted = {MATCH[flag]: value for flag, value in options.items() if flag in MATCH}
        return [item for item in self.items if all(item[name] == value for name, value in wanted.items())]

    def __call__(self, argv, input):
        if self.mode == 'locked':
            return Completed(36, b'', b'security: SecKeychainItemCopyContent: User interaction is not allowed.\n')
        if self.mode == 'fails':
            return Completed(1, b'', b'security: something went wrong with ' + MARKER.encode() + b'\n')
        if argv[1] == 'dump-keychain':
            self.dumps.append(list(argv))
            if len(argv) != 2:
                return Completed(2, b'', b'usage\n')               # the store asks for attributes only
            return Completed(0, self.dump(), b'')
        if argv[1] == '-i':
            for line in input.decode('ascii').splitlines():
                words = shlex.split(line)
                options = self._options(words[1:])
                if words[0] != 'add-generic-password' or '-U' in words:
                    continue                                     # -i goes on whatever a line does
                item = {name: options.get(flag) for flag, name in MATCH.items()}
                item['labl'], item['password'] = options.get('-l'), bytes.fromhex(options['-X'])
                if any(old['acct'] == item['acct'] and old['svce'] == item['svce'] for old in self.items):
                    continue                                     # "already exists": not replaced without -U
                if self.mode == 'mangles':
                    item['password'] = item['password'][:-1]
                if self.mode != 'drops':
                    self.items.append(item)
            return Completed(0, b'', b'')
        options = self._options(argv[2:])
        found = self._matches(options)
        if argv[1] == 'find-generic-password':
            if not found:
                return Completed(44, b'', b'security: SecKeychainSearchCopyNext: The specified item could not be '
                                          b'found in the keychain.\n')
            if '-w' in argv:
                return Completed(0, found[0]['password'] + b'\n', b'')
            return Completed(0, b'keychain: "login.keychain-db"\nclass: "genp"\n', b'')
        if argv[1] == 'delete-generic-password':
            if not found:
                return Completed(44, b'', b'security: The specified item could not be found in the keychain.\n')
            if self.mode != 'stuck':
                self.items.remove(found[0])
            return Completed(0, b'password has been deleted.\n', b'')
        return Completed(2, b'', b'usage\n')

    OTHERS = ('keychain: "/Users/someone/Library/Keychains/login.keychain-db"\nversion: 512\nclass: "inet"\n'
              'attributes:\n    "acct"<blob>="codex-key"\n    "srvr"<blob>="api.example.org"\n'
              'keychain: "/Users/someone/Library/Keychains/login.keychain-db"\nversion: 512\nclass: "genp"\n'
              'attributes:\n    0x00000007 <blob>="Example Mail"\n    "acct"<blob>="codex-key"\n'
              '    "gena"<blob>=<NULL>\n    "svce"<blob>="com.example.mail"\n'
              'keychain: "/Users/someone/Library/Keychains/login.keychain-db"\nversion: 512\nclass: "genp"\n'
              'attributes:\n    "acct"<blob>="codex-key"\n    "svce"<blob>="recursion.not-a-save"\n')

    def dump(self) -> bytes:
        '''``security dump-keychain`` without ``-d``: each item's attributes, never its password.'''
        if self.mode == 'garbled':
            return b'not a keychain dump\n'
        text = '' if self.mode == 'bare' else self.OTHERS
        for item in self.items:
            service = item['svce']
            shown = f'0x{service.encode().hex().upper()}  "{service}"' if self.mode == 'hexform' else f'"{service}"'
            text += ('keychain: "/Users/someone/Library/Keychains/login.keychain-db"\nversion: 512\nclass: "genp"\n'
                     f'attributes:\n    0x00000007 <blob>="{item["labl"]}"\n    0x00000008 <blob>=<NULL>\n'
                     f'    "acct"<blob>="{item["acct"]}"\n'
                     '    "cdat"<timedate>=0x32303235313131343232343030305A00  "20251114224000Z\\000"\n'
                     f'    "gena"<blob>="{item["gena"]}"\n    "icmt"<blob>="{item["icmt"]}"\n'
                     f'    "svce"<blob>={shown}\n    "type"<uint32>=<NULL>\n')
        return text.encode()


def mac_system():
    security = FakeSecurity()
    return FakeSystem(platform='darwin', tools={'security': security}), security


def fixed_clock() -> float:
    return 1_763_160_000.25


@needs_posix
class MacKeychainTest(TempStoreCase):
    def mac_store(self, name='keys'):
        system, security = mac_system()
        return KeyStore(self.base / name / 'keys', system=system, clock=fixed_clock), system, security

    def test_round_trip_with_the_key_never_in_the_arguments(self) -> None:
        store, system, security = self.mac_store()
        record = self.add(store)
        index = store.list()
        self.assertEqual(index.backend, 'macos_keychain')
        self.assertFalse((store.root / 'secrets.json').exists())
        self.assertEqual(security.items, [{'acct': 'codex-key', 'svce': f'recursion.{record.save_id}',
                                           'gena': index.store_id, 'icmt': 'openai',
                                           'labl': 'Recursion API key codex-key', 'password': MARKER.encode()}])
        self.assertEqual(store.credential('codex-key', 'CODEX_API_KEY'), MARKER)
        self.assertEqual(store.remove('codex-key').key_id, 'codex-key')
        self.assertEqual(security.items, [])
        for argv, stdin in system.calls:
            self.assertNotIn(MARKER, ' '.join(argv))
            self.assertNotIn(MARKER.encode().hex(), ' '.join(argv))
            if stdin is not None:
                self.assertNotIn(MARKER.encode(), stdin)                 # only its hexadecimal form
                self.assertIn(MARKER.encode().hex().encode(), stdin)

    def test_a_value_is_released_only_to_the_record_of_its_save(self) -> None:
        store, _, security = self.mac_store()
        self.add(store)
        older = (store.root / 'index.json').read_bytes()
        store.remove('codex-key')
        self.add(store, value=OTHER)                                     # another save of the same id
        (store.root / 'index.json').write_bytes(older)                   # the older index put back
        with self.assertRaises(KeyRefused) as refused:
            store.credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'secret_missing')       # never the other save's value
        self.assertEqual(store.remove('codex-key').key_id, 'codex-key')  # every save of the id goes
        self.assertEqual(security.items, [])

    def test_a_save_or_a_removal_touches_only_its_own_id_and_store(self) -> None:
        store, system, security = self.mac_store()
        other = KeyStore(self.base / 'other' / 'keys', system=system, clock=fixed_clock)
        self.add(store, key_id='alpha')
        self.add(store, key_id='beta', value=OTHER, env='ANTHROPIC_API_KEY', provider='anthropic')
        self.add(other, key_id='alpha', value=OTHER)                     # the same id in another store
        store.remove('alpha')
        self.assertEqual(sorted((item['acct'], item['gena']) for item in security.items),
                         sorted([('beta', store.list().store_id), ('alpha', other.list().store_id)]))
        self.assertEqual(other.credential('alpha', 'CODEX_API_KEY'), OTHER)
        self.assertEqual(store.credential('beta', 'ANTHROPIC_API_KEY'), OTHER)

    def test_no_save_replaces_another_and_a_removal_takes_every_save_of_the_id(self) -> None:
        store, _, security = self.mac_store()
        self.add(store)
        store_id = store.list().store_id
        (store.root / 'index.json').write_bytes(index_bytes(Index(None, {}, {}, store_id)))     # emptied
        self.add(store, value=OTHER)
        self.assertEqual(sorted(item['password'] for item in security.items), sorted([MARKER.encode(), OTHER.encode()]))
        self.assertEqual(store.credential('codex-key', 'CODEX_API_KEY'), OTHER)
        store.remove('codex-key')
        self.assertEqual(security.items, [])

    def test_a_save_the_keychain_did_not_keep_is_refused_and_only_its_own_item_taken_out(self) -> None:
        for mode in ('drops', 'mangles'):
            with self.subTest(mode=mode):
                store, _, security = self.mac_store(mode)
                security.mode = mode
                with self.assertRaises(KeyStoreError) as caught:
                    self.add(store)
                self.assertEqual(caught.exception.code, 'keychain_failed')
                self.assertEqual((security.items, store.list().keys, store.list().pending), ([], {}, {}))
        store, _, security = self.mac_store('orphan')
        self.add(store, key_id='alpha')
        store_id = store.list().store_id
        (store.root / 'index.json').write_bytes(index_bytes(Index(None, {}, {}, store_id)))     # emptied
        security.mode = 'mangles'
        with self.assertRaises(KeyStoreError):
            self.add(store, key_id='alpha', value=OTHER)
        self.assertEqual([item['password'] for item in security.items], [MARKER.encode()])     # the earlier save's
        self.assertEqual(sorted(store.list().pending), ['alpha'])        # its item is there: the intent stays
        security.mode = 'ok'
        self.assertIsNone(store.remove('alpha'))
        self.assertEqual(security.items, [])

    def test_a_locked_keychain_is_refused_and_never_answered_from_the_file(self) -> None:
        store, _, security = self.mac_store()
        self.add(store)
        security.mode = 'locked'
        with self.assertRaises(KeyRefused) as refused:
            store.credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'keychain_locked')
        with self.assertRaises(KeyStoreError) as caught:
            self.add(store, key_id='second', value=OTHER)
        self.assertEqual(caught.exception.code, 'keychain_locked')
        self.assertFalse((store.root / 'secrets.json').exists())

    def test_a_vanished_item_is_secret_missing_and_can_still_be_removed(self) -> None:
        store, _, security = self.mac_store()
        self.add(store)
        security.items.clear()
        with self.assertRaises(KeyRefused) as refused:
            store.credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'secret_missing')
        store.remove('codex-key')
        self.assertEqual(store.list().keys, {})

    def test_other_failures_are_keychain_failed_with_fixed_text(self) -> None:
        store, _, security = self.mac_store()
        security.mode = 'fails'
        with self.assertRaises(KeyStoreError) as caught:
            self.add(store)
        self.assertEqual(caught.exception.code, 'keychain_failed')
        self.assertNotIn(MARKER, str(caught.exception))
        self.assertNotIn(MARKER.encode().hex(), str(caught.exception))
        self.assertNotIn('something went wrong', str(caught.exception))  # nothing the tool printed

    def test_an_item_that_does_not_go_keeps_its_record(self) -> None:
        store, _, security = self.mac_store()
        self.add(store)
        security.mode = 'stuck'
        with self.assertRaises(KeyStoreError) as caught:
            store.remove('codex-key')
        self.assertEqual(caught.exception.code, 'keychain_failed')
        self.assertIn('codex-key', store.list().keys)
        self.assertEqual(json.loads((store.root / 'index.json').read_text())['pending']['codex-key']['op'], 'remove')


def run_main(store, *argv):
    out, err = io.StringIO(), io.StringIO()
    status = entry.main(list(argv), out=out, err=err, store=store)
    return status, out.getvalue() + err.getvalue()


@needs_posix
class MacKeychainAuditTest(TempStoreCase):
    """The audit counts the items no record names on the macOS Keychain too (``unnamed_items``, through
    ``dump-keychain``): an index lost, emptied or put back, or another store's items, never leave it complete. Found
    by lane KEYS' own attack on pull request 2 before its first push."""

    def mac_store(self, name='keys', system=None):
        if system is None:
            system, security = mac_system()
        else:
            security = system.tools['security']
        return KeyStore(self.base / name / 'keys', system=system, clock=fixed_clock), security

    def test_an_emptied_index_leaves_items_the_audit_counts_until_a_removal_takes_them(self) -> None:
        store, _ = self.mac_store()
        self.add(store)
        self.add(store, key_id='beta', value=OTHER, env='ANTHROPIC_API_KEY', provider='anthropic')
        store_id = store.list().store_id
        (store.root / 'index.json').write_bytes(index_bytes(Index(None, {}, {}, store_id)))     # emptied
        self.assertEqual(store.unnamed_items(), 2)
        status, text = run_main(store, 'audit', '--no-journal')
        self.assertEqual(status, 4)
        self.assertIn('keychain: 2 item(s) in the macOS Keychain that Recursion saved and no saved key of this store '
                      'names', text)
        self.assertIn('Keychain Access finds the rest by the name "Recursion API key <id>"', text)
        self.assertNotIn(MARKER, text)
        self.add(store)                                   # a new save of the id: the old save's item is still no one's
        self.assertEqual(store.unnamed_items(), 2)
        store.remove('codex-key')                         # every save of the id in this store
        self.assertEqual(store.unnamed_items(), 1)        # beta's, which no saved key names

    def test_a_lost_index_leaves_an_item_the_audit_counts(self) -> None:
        store, _ = self.mac_store()
        self.add(store)
        (store.root / 'index.json').unlink()
        self.assertEqual(store.unnamed_items(), 1)
        self.assertEqual(run_main(store, 'audit', '--no-journal')[0], 4)
        self.add(store, value=OTHER)                      # a new store id: the old item is named by nothing
        self.assertEqual(store.unnamed_items(), 1)
        self.assertEqual(store.credential('codex-key', 'CODEX_API_KEY'), OTHER)

    def test_a_put_back_index_leaves_the_newer_saves_item_counted(self) -> None:
        store, _ = self.mac_store()
        self.add(store)
        older = (store.root / 'index.json').read_bytes()
        store.remove('codex-key')
        self.add(store, value=OTHER)
        (store.root / 'index.json').write_bytes(older)
        self.assertEqual(store.unnamed_items(), 1)
        self.assertEqual(run_main(store, 'audit', '--no-journal')[0], 4)

    def test_another_stores_items_are_counted_as_on_the_secret_service(self) -> None:
        system, _ = mac_system()
        one, _ = self.mac_store('one', system)
        two, _ = self.mac_store('two', system)
        self.add(one)
        self.add(two, value=OTHER)
        self.assertEqual((one.unnamed_items(), two.unnamed_items()), (1, 1))      # questions.md K20, option (a)

    def test_the_count_reads_attributes_only_and_counts_no_other_programs_item(self) -> None:
        store, security = self.mac_store()
        self.add(store)
        self.assertEqual(store.unnamed_items(), 0)        # beside items of other programs under the same account
        self.assertTrue(security.dumps)
        self.assertTrue(all(argv == ['/fake/bin/security', 'dump-keychain'] for argv in security.dumps))   # no -d
        self.assertNotIn(MARKER.encode(), security.dump())
        report = audit_mod.run(store, journal=False)
        self.assertEqual((report.unnamed, report.unnamed_refused, report.complete()), (0, None, True))

    def test_a_dump_that_fails_or_cannot_be_read_is_refused_and_the_audit_incomplete(self) -> None:
        for mode, code in (('locked', 'keychain_locked'), ('fails', 'keychain_failed'), ('garbled', 'keychain_failed')):
            with self.subTest(mode=mode):
                store, security = self.mac_store(mode)
                self.add(store)
                security.mode = mode
                with self.assertRaises(KeyStoreError) as caught:
                    store.unnamed_items()
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn(MARKER, str(caught.exception))
                report = audit_mod.run(store, journal=False)
                self.assertEqual(report.unnamed_refused, code)
                self.assertFalse(report.complete())

    def test_an_empty_keychain_counts_none_and_a_service_printed_as_hexadecimal_is_read(self) -> None:
        store, security = self.mac_store()
        security.mode = 'bare'
        self.assertEqual(store.unnamed_items(), 0)        # an empty dump is an empty keychain, not a refusal
        security.mode = 'ok'
        self.add(store)
        (store.root / 'index.json').unlink()
        security.mode = 'hexform'
        self.assertEqual(store.unnamed_items(), 1)

    def test_an_unfinished_operation_names_only_its_own_stores_items_of_the_id(self) -> None:
        system, security = mac_system()
        one, _ = self.mac_store('one', system)
        two, _ = self.mac_store('two', system)
        self.add(one)
        self.add(two, value=OTHER)                        # the same id in another store
        security.mode = 'stuck'
        with self.assertRaises(KeyStoreError):
            one.remove('codex-key')                       # its item stays, and the removal stays unfinished
        security.mode = 'ok'
        self.assertEqual(sorted(one.list().pending), ['codex-key'])
        self.assertEqual(one.unnamed_items(), 1)          # the other store's item: the operation names only its own
