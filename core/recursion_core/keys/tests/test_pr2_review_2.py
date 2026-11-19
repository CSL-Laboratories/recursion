"""The second reads of pull request 2 (#422): Codex astra's required changes 1 to 5 and DeepSeek V4.1 Flash's F1 to
F3. Every key is made up; the macOS tests use the stand-in ``security`` of ``test_macos.py``."""
from __future__ import annotations

import io
import json
from dataclasses import replace

from recursion_core.keys import __main__ as entry
from recursion_core.keys import audit as audit_mod
from recursion_core.keys.backends import MacKeychain
from recursion_core.keys.errors import KeyRefused, KeyStoreError
from recursion_core.keys.store import INDEX_MAX, KeyRecord, KeyStore, index_bytes
from recursion_core.keys.system import Completed
from recursion_core.keys.tests.support import (
    MARKER,
    FakeSystem,
    TempStoreCase,
    needs_posix,
)
from recursion_core.keys.tests.test_macos import (
    ELSEWHERE,
    LOGIN,
    FakeSecurity,
    fixed_clock,
)
from recursion_core.keys.tests.test_pr2_review_1 import SpyStdin, SpyTerminal

ADD = ('--provider', 'openai', '--env', 'CODEX_API_KEY')


class MacCase(TempStoreCase):
    def mac(self, name='keys'):
        security = FakeSecurity()
        system = FakeSystem(platform='darwin', tools={'security': security})
        return KeyStore(self.base / name / 'keys', system=system, clock=fixed_clock), system, security


@needs_posix
class ChangedLoginKeychainTest(MacCase):
    """astra 1: a store pinned to the macOS Keychain records the login keychain it saved in; when another keychain is
    the login keychain later, nothing of the store's is looked for or removed there, and nothing is dropped."""

    def test_the_index_records_the_keychain_and_forgets_it_with_its_last_key(self) -> None:
        store, _, _ = self.mac()
        self.add(store)
        self.assertEqual(store.list().keychain, LOGIN)
        self.assertEqual(json.loads((store.root / 'index.json').read_text())['keychain'], LOGIN)
        store.remove('codex-key')
        self.assertNotIn('keychain', json.loads((store.root / 'index.json').read_text()))

    def test_a_removal_after_another_keychain_became_the_login_keychain_is_refused_and_keeps_the_record(self) -> None:
        store, _, security = self.mac()
        self.add(store)
        security.login = ELSEWHERE
        with self.assertRaises(KeyStoreError) as caught:
            store.remove('codex-key')
        self.assertEqual(caught.exception.code, 'keychain_unavailable')
        self.assertIn('codex-key', store.list().keys)
        self.assertEqual(store.list().pending['codex-key'].op, 'remove')
        self.assertEqual([item['keychain'] for item in security.items], [LOGIN])
        self.assertFalse(audit_mod.run(store, journal=False).complete())
        with self.assertRaises(KeyRefused) as refused:
            store.credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'operation_pending')
        security.login = LOGIN                                           # the keychain is the login one again
        store.remove('codex-key')
        self.assertEqual((security.items, store.list().keys, store.list().pending), ([], {}, {}))

    def test_an_unfinished_add_keeps_its_intent_when_the_login_keychain_changed(self) -> None:
        store, _, security = self.mac()
        security.mode = 'writes_then_fails'
        with self.assertRaises(KeyStoreError):
            self.add(store)
        self.assertEqual(sorted(store.list().pending), ['codex-key'])
        security.mode, security.login = 'ok', ELSEWHERE
        with self.assertRaises(KeyStoreError):
            store.remove('codex-key')
        self.assertEqual(sorted(store.list().pending), ['codex-key'])     # absence elsewhere proves nothing here
        self.assertEqual(len(security.items), 1)

    def test_a_save_beside_a_recorded_keychain_that_is_no_longer_the_login_one_is_refused_first(self) -> None:
        store, system, security = self.mac()
        self.add(store)
        security.login = ELSEWHERE
        system.calls.clear()
        with self.assertRaises(KeyStoreError) as caught:
            self.add(store, key_id='second', value=MARKER[::-1])
        self.assertEqual(caught.exception.code, 'keychain_unavailable')
        self.assertEqual([argv[1] for argv, _ in system.calls], ['login-keychain'])     # before any line or intent
        self.assertEqual((sorted(store.list().keys), store.list().pending), (['codex-key'], {}))

    def test_the_login_keychain_is_asked_once_per_operation(self) -> None:
        store, system, security = self.mac()
        original = security.__call__

        def changes_after_the_save(argv, input):
            done = original(argv, input)
            if argv[1] == '-i':
                security.login = ELSEWHERE                              # changes mid-operation: not looked at again
            return done

        system.tools['security'] = changes_after_the_save
        self.add(store)
        self.assertEqual([item['keychain'] for item in security.items], [LOGIN])
        self.assertEqual(sum(argv[1] == 'login-keychain' for argv, _ in system.calls), 1)

    def test_an_index_whose_keychain_is_not_a_live_macos_pin_or_not_a_path_is_refused(self) -> None:
        store, _, _ = self.mac()
        self.add(store)
        index = store.list()
        for bad in ('relative/login.keychain-db', '/a"quote', '/back\\slash', '/new\nline', 7):
            with self.subTest(keychain=bad):
                doc = json.loads(index_bytes(index))
                doc['keychain'] = bad
                (store.root / 'index.json').write_text(json.dumps(doc, indent=1) + '\n')
                with self.assertRaises(KeyStoreError) as caught:
                    store.list()
                self.assertEqual(caught.exception.code, 'index_invalid')
        doc = json.loads(index_bytes(replace(index, backend='owner_file', keys={
            kid: replace(record, store='owner_file') for kid, record in index.keys.items()})))
        doc['keychain'] = LOGIN                                          # a keychain beside the file pin
        (store.root / 'index.json').write_text(json.dumps(doc, indent=1) + '\n')
        with self.assertRaises(KeyStoreError) as caught:
            store.list()
        self.assertEqual(caught.exception.code, 'index_invalid')


@needs_posix
class AbsenceOverEverythingDeleteSelectsTest(MacCase):
    """astra 2 / DeepSeek F1 (MEDIUM): ``delete`` selects by account and store, whatever the service; absence is
    confirmed over the same items, and the audit counts #422's first-head services as Recursion's."""

    def test_an_item_in_the_first_heads_service_keeps_its_record_when_the_search_fails(self) -> None:
        store, _, security = self.mac()
        record = self.add(store)
        security.items[0]['svce'] = f'recursion.{record.save_id}'        # as 2c38fa66 wrote it
        security.mode = 'ambiguous'
        with self.assertRaises(KeyStoreError) as caught:
            store.remove('codex-key')
        self.assertEqual(caught.exception.code, 'keychain_failed')
        self.assertIn('codex-key', store.list().keys)
        self.assertEqual(len(security.items), 1)

    def test_an_item_of_another_service_under_the_same_account_and_store_is_not_absence(self) -> None:
        store, _, security = self.mac()
        self.add(store)
        store_id = store.list().store_id
        security.items[:] = [{'keychain': LOGIN, 'acct': 'codex-key', 'svce': 'com.example.other', 'gena': store_id,
                              'icmt': None, 'labl': 'other', 'password': b'made-up-other-value'}]
        security.mode = 'ambiguous'
        with self.assertRaises(KeyStoreError):
            store.remove('codex-key')
        self.assertIn('codex-key', store.list().keys)

    def test_the_audit_counts_an_item_in_the_first_heads_service(self) -> None:
        store, _, security = self.mac()
        record = self.add(store)
        security.items[0]['svce'] = f'recursion.{record.save_id}'
        (store.root / 'index.json').unlink()
        self.assertEqual(store.unnamed_items(), 1)
        self.assertFalse(audit_mod.run(store, journal=False).complete())


@needs_posix
class UnencodableFolderPathTest(TempStoreCase):
    """astra 3 / DeepSeek F2: a lone surrogate in the folder path is folder_invalid, as a NUL byte is."""

    def test_every_command_refuses_it_typed_and_describe_reports_it(self) -> None:
        before = sorted(self.base.iterdir())
        for char in ('\ud800', '\udc01'):
            with self.subTest(char=hex(ord(char))):
                store = KeyStore(self.base / f'a{char}b' / 'keys', system=FakeSystem())
                self.assertEqual(store.describe()['refusal']['code'], 'folder_invalid')
                with self.assertRaises(KeyRefused) as refused:
                    store.spend_policy('codex-key')
                self.assertEqual(refused.exception.code, 'folder_invalid')
                self.assertIsNone(refused.exception.__context__)
                for call in (store.list, store.unnamed_items,
                             lambda store=store: store.check_add('codex-key', 'openai', 'X'),
                             lambda store=store: store.add('codex-key', 'openai', 'CODEX_API_KEY', MARKER,
                                                           metered_spend_allowed=True, cap_usd=5)):
                    with self.assertRaises(KeyStoreError) as caught:
                        call()
                    self.assertEqual(caught.exception.code, 'folder_invalid')
        self.assertEqual(sorted(self.base.iterdir()), before)


@needs_posix
class FullIndexTest(TempStoreCase):
    """astra 4 / DeepSeek F3: an index already too full for one more key is refused before the key is read."""

    def full_store(self) -> KeyStore:
        store = KeyStore(self.base / 'full' / 'keys', system=FakeSystem())
        self.add(store)
        index = store.list()

        def fill(count):
            records = {f'fill{i:05d}': KeyRecord(f'fill{i:05d}', 'openai', 'CODEX_API_KEY', '2026-11-15T00:00:00.000Z',
                                                 'owner_file', False, 0, f'{i + 1:032x}') for i in range(count)}
            return replace(index, keys={**index.keys, **records})

        size = [len(index_bytes(fill(n))) for n in (0, 1, 2)]
        count = (INDEX_MAX - size[0]) // (size[2] - size[1])
        while len(index_bytes(fill(count))) > INDEX_MAX:
            count -= 1
        (store.root / 'index.json').write_bytes(index_bytes(fill(count)))
        self.assertGreater(len(store.list().keys), count)                 # still readable
        return store

    def test_check_add_refuses_store_full(self) -> None:
        store = self.full_store()
        with self.assertRaises(KeyStoreError) as caught:
            store.check_add('new-key', 'openai', 'CODEX_API_KEY')
        self.assertEqual(caught.exception.code, 'store_full')

    def test_key_add_asks_for_nothing(self) -> None:
        store = self.full_store()
        for extra in ((), ('--stdin',)):
            with self.subTest(extra=extra):
                terminal, stdin, out, err = SpyTerminal(), SpyStdin(), io.StringIO(), io.StringIO()
                status = entry.main(['add', 'new-key', *ADD, *extra], out=out, err=err, store=store,
                                    terminal=terminal, stdin=stdin)
                self.assertEqual(status, 1)
                self.assertTrue(err.getvalue().startswith('refused: store_full: '), err.getvalue())
                self.assertEqual((terminal.prompts, stdin.reads), ([], 0))


@needs_posix
class ListingCutShortTest(MacCase):
    """astra 5: a listing that lost an item's class, account, service, generic attribute or comment is refused."""

    def test_each_required_line_missing_is_refused(self) -> None:
        for omitted in ('gena', 'icmt', 'acct', 'svce', 'class'):
            with self.subTest(omitted=omitted):
                store, system, security = self.mac(omitted)
                self.add(store)
                (store.root / 'index.json').unlink()
                prefix = b'class: ' if omitted == 'class' else f'    "{omitted}"'.encode()
                cut = b'\n'.join(line for line in security.dump().splitlines() if not line.startswith(prefix))

                def tool(argv, input, cut=cut, security=security):
                    return Completed(0, cut, b'') if argv[1] == 'dump-keychain' else security(argv, input)

                system.tools['security'] = tool
                with self.assertRaises(KeyStoreError) as caught:
                    store.unnamed_items()
                self.assertEqual(caught.exception.code, 'keychain_failed')


@needs_posix
class EncodedLineBoundaryTest(MacCase):
    """astra 5: the line limit counts bytes, so a keychain path in a script of two bytes a character shortens it."""

    def test_the_longest_key_fits_and_one_more_is_refused_with_a_multibyte_path(self) -> None:
        path = '/Users/' + 'é' * 70 + '/Library/Keychains/login.keychain-db'
        store, system, security = self.mac()

        def tool(argv, input):
            if argv[1] == 'login-keychain':
                return Completed(0, f'    "{path}"\n'.encode(), b'')
            if argv[1] == '-i':
                input = input.replace(path.encode(), LOGIN.encode())
            return security([LOGIN if word == path else word for word in argv], input)

        system.tools['security'] = tool
        key_id, provider = ('alpha-' * 11)[:64], 'p' * 64
        longest = MacKeychain(system, '0' * 32).longest(key_id, provider)
        self.add(store, key_id=key_id, provider=provider, value='x' * longest)
        lines = [stdin for argv, stdin in system.calls if argv[1] == '-i']
        self.assertTrue(lines and all(len(stdin) <= 4095 for stdin in lines))
        self.assertIn(max(len(stdin) for stdin in lines), (4094, 4095))   # at the boundary, in bytes (the hex
        # is two bytes a character, so the line can end one byte short)
        store.remove(key_id)
        system.calls.clear()
        with self.assertRaises(KeyStoreError) as caught:
            self.add(store, key_id=key_id, provider=provider, value='x' * (longest + 1))
        self.assertEqual(caught.exception.code, 'value_invalid')
        self.assertFalse(any(argv[1] == '-i' for argv, _ in system.calls))
        self.assertEqual(store.list().pending, {})


@needs_posix
class NativeFallbackTest(MacCase):
    """sol 1: given a keychain it cannot open, Apple's security falls back to the search list and says so on standard
    error; an answer that came that way is not taken, and nothing is dropped on it."""

    def test_a_save_whose_read_back_came_from_a_fallback_is_refused_and_keeps_its_intent(self) -> None:
        store, system, security = self.mac()
        vanished = '/Users/someone/Library/Keychains/vanished.keychain-db'
        original = security.__call__

        def login_names_a_missing_keychain(argv, input):
            if argv[1] == 'login-keychain':
                return Completed(0, f'    "{vanished}"\n'.encode(), b'')
            return original(argv, input)

        system.tools['security'] = login_names_a_missing_keychain
        with self.assertRaises(KeyStoreError) as caught:
            self.add(store)
        self.assertEqual(caught.exception.code, 'keychain_failed')
        self.assertNotIn(MARKER, str(caught.exception))
        self.assertEqual(sorted(store.list().pending), ['codex-key'])     # the item may be somewhere: kept
        self.assertEqual(store.list().keys, {})

    def test_a_login_keychain_answer_with_an_error_beside_it_is_refused(self) -> None:
        store, system, security = self.mac()
        original = security.__call__

        def noisy_login(argv, input):
            done = original(argv, input)
            if argv[1] == 'login-keychain':
                return Completed(0, done.stdout, b'security: SecKeychainCopyLogin: an error\n')
            return done

        system.tools['security'] = noisy_login
        with self.assertRaises(KeyStoreError) as caught:
            self.add(store)
        self.assertEqual(caught.exception.code, 'keychain_failed')
        self.assertEqual((store.list().pending, security.items), ({}, []))

    def test_a_deletion_that_fell_back_is_refused_and_keeps_the_record(self) -> None:
        from recursion_core.keys.tests.test_macos import OPEN_FAILED

        store, system, security = self.mac()
        self.add(store)
        original = security.__call__

        def deletion_falls_back(argv, input):
            done = original(argv, input)
            if argv[1] == 'delete-generic-password' and done.returncode == 0:
                return Completed(0, done.stdout, OPEN_FAILED + done.stderr)     # as if another keychain answered
            return done

        system.tools['security'] = deletion_falls_back
        with self.assertRaises(KeyStoreError) as caught:
            store.remove('codex-key')
        self.assertEqual(caught.exception.code, 'keychain_failed')
        self.assertIn('codex-key', store.list().keys)
        self.assertEqual(store.list().pending['codex-key'].op, 'remove')

    def test_the_stand_in_falls_back_and_says_so(self) -> None:
        security = FakeSecurity()
        done = security(['/usr/bin/security', 'dump-keychain', '/nowhere.keychain-db'], None)
        self.assertEqual(done.returncode, 0)
        self.assertIn(b'SecKeychainOpen', done.stderr)


@needs_posix
class InvalidLoginPathTest(MacCase):
    """sol 5: a login-keychain path that is not absolute, or holds a quote, a backslash or a control character, is
    refused before the store writes anything."""

    def test_each_is_refused_before_the_intent(self) -> None:
        paths = ('relative.keychain-db', '/a/with"quote', '/a/with\\backslash', '/a/with\tcontrol')
        for number, path in enumerate(paths):
            with self.subTest(path=path):
                store, system, security = self.mac(f'path-{number}')
                original = security.__call__

                def tool(argv, input, path=path, original=original):
                    if argv[1] == 'login-keychain':
                        return Completed(0, f'"{path}"\n'.encode(), b'')
                    return original(argv, input)

                system.tools['security'] = tool
                with self.assertRaises(KeyStoreError) as caught:
                    self.add(store)
                self.assertEqual(caught.exception.code, 'keychain_failed')
                self.assertFalse(any(argv[1] == '-i' for argv, _ in system.calls))
                self.assertEqual((store.list().pending, security.items), ({}, []))


@needs_posix
class FullIndexWithACapTest(TempStoreCase):
    """sol 4: the pre-check sizes the record the command line describes: room for the smallest record is not room
    for one with a cap."""

    def test_room_for_the_smallest_record_is_not_room_for_a_capped_one(self) -> None:
        from recursion_core.keys.store import Pending

        store = KeyStore(self.base / 'edge' / 'keys', system=FakeSystem())
        self.add(store)
        index = store.list()
        stamp = '2026-11-15T00:00:00.000Z'

        def fill(count, pads=(1, 1, 1)):
            # The first fillers' variable names take the padding, at most 128 characters each, one byte a character.
            records = {f'fill{i:05d}': KeyRecord(f'fill{i:05d}', 'openai', 'P' * pads[i] if i < len(pads) else
                                                 'CODEX_API_KEY', stamp, 'owner_file', False, 0, f'{i + 1:032x}')
                       for i in range(count)}
            return replace(index, keys={**index.keys, **records})

        def with_new(filled, allowed, cap):
            record = KeyRecord('new-key', 'openai', 'CODEX_API_KEY', stamp, 'owner_file', allowed, cap, '0' * 32)
            return len(index_bytes(replace(filled, keys={**filled.keys, 'new-key': record},
                                           pending={'new-key': Pending('new-key', 'add', stamp)})))

        per = len(index_bytes(fill(5))) - len(index_bytes(fill(4)))
        count = (INDEX_MAX - len(index_bytes(index))) // per
        while with_new(fill(count), False, 0) > INDEX_MAX:
            count -= 1
        while with_new(fill(count + 1), False, 0) <= INDEX_MAX:
            count += 1
        room = INDEX_MAX - with_new(fill(count), False, 0)          # less than one filler record
        pads = []
        for _ in range(3):
            pads.append(1 + min(room, 127))
            room -= pads[-1] - 1
        self.assertEqual(room, 0)
        filled = fill(count, tuple(pads))
        self.assertEqual(with_new(filled, False, 0), INDEX_MAX)
        self.assertGreater(with_new(filled, True, 99999.99), INDEX_MAX)
        (store.root / 'index.json').write_bytes(index_bytes(filled))
        store.check_add('new-key', 'openai', 'CODEX_API_KEY')                       # the smallest record fits
        with self.assertRaises(KeyStoreError) as caught:
            store.check_add('new-key', 'openai', 'CODEX_API_KEY', metered_spend_allowed=True, cap_usd=99999.99)
        self.assertEqual(caught.exception.code, 'store_full')
        terminal, stdin, out, err = SpyTerminal(), SpyStdin(), io.StringIO(), io.StringIO()
        status = entry.main(['add', 'new-key', *ADD, '--allow-metered-spend', '--cap-usd', '99999.99', '--stdin'],
                            out=out, err=err, store=store, terminal=terminal, stdin=stdin)
        self.assertEqual(status, 1)
        self.assertTrue(err.getvalue().startswith('refused: store_full: '), err.getvalue())
        self.assertEqual((terminal.prompts, stdin.reads), ([], 0))
