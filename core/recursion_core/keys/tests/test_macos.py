"""The macOS Keychain backend, against a stand-in ``security`` (no Mac here, so it has not run on one).

The stand-in parses what the backend sends exactly as ``security`` documents it: ``-i`` with one
``add-generic-password -U -a <id> -s recursion -l "..." -X <hex>`` line on standard input,
``find-generic-password -a <id> -s recursion -w`` printing the password and a newline, ``delete-generic-password``,
and exit 44 for an item that is not there.
"""
from __future__ import annotations

import shlex

from recursion_core.keys.errors import KeyRefused, KeyStoreError
from recursion_core.keys.system import Completed
from recursion_core.keys.tests.support import (
    MARKER,
    OTHER,
    FakeSystem,
    TempStoreCase,
    needs_posix,
)


class FakeSecurity:
    def __init__(self) -> None:
        self.items: dict = {}       # (service, account) -> password bytes
        self.mode = 'ok'            # ok | locked | drops | mangles | fails

    @staticmethod
    def _opt(words, flag):
        return words[words.index(flag) + 1]

    def __call__(self, argv, input):
        if self.mode == 'locked':
            return Completed(36, b'', b'security: SecKeychainItemCopyContent: User interaction is not allowed.\n')
        if self.mode == 'fails':
            return Completed(1, b'', b'security: something went wrong\n')
        if argv[1] == '-i':
            for line in input.decode('ascii').splitlines():
                words = shlex.split(line)
                if words[0] != 'add-generic-password' or '-U' not in words:
                    return Completed(0, b'', b'unknown command\n')     # -i keeps going whatever a line does
                password = bytes.fromhex(self._opt(words, '-X'))
                if self.mode == 'mangles':
                    password = password[:-1]
                if self.mode != 'drops':
                    self.items[(self._opt(words, '-s'), self._opt(words, '-a'))] = password
            return Completed(0, b'', b'')
        key = (self._opt(argv, '-s'), self._opt(argv, '-a'))
        if argv[1] == 'find-generic-password':
            if key not in self.items:
                return Completed(44, b'', b'security: SecKeychainSearchCopyNext: The specified item could not be '
                                          b'found in the keychain.\n')
            return Completed(0, self.items[key] + b'\n', b'')
        if argv[1] == 'delete-generic-password':
            if self.items.pop(key, None) is None:
                return Completed(44, b'', b'security: The specified item could not be found in the keychain.\n')
            return Completed(0, b'password has been deleted.\n', b'')
        return Completed(2, b'', b'usage\n')


def mac_system():
    security = FakeSecurity()
    return FakeSystem(platform='darwin', tools={'security': security}), security


@needs_posix
class MacKeychainTest(TempStoreCase):
    def test_round_trip_with_the_key_never_in_the_arguments(self) -> None:
        system, security = mac_system()
        store = self.store(system)
        self.add(store)
        self.assertEqual(store.list().backend, 'macos_keychain')
        self.assertFalse((self.root / 'secrets.json').exists())
        self.assertEqual(security.items, {('recursion', 'codex-key'): MARKER.encode()})
        self.assertEqual(store.credential('codex-key', 'CODEX_API_KEY'), MARKER)
        store.remove('codex-key')
        self.assertEqual(security.items, {})
        for argv, stdin in system.calls:
            self.assertNotIn(MARKER, ' '.join(argv))
            self.assertNotIn(MARKER.encode().hex(), ' '.join(argv))
            if stdin is not None:
                self.assertNotIn(MARKER.encode(), stdin)                 # only its hexadecimal form
                self.assertIn(MARKER.encode().hex().encode(), stdin)
        self.assertEqual([argv[1] for argv, _ in system.calls],
                         ['-i', 'find-generic-password', 'find-generic-password', 'delete-generic-password'])

    def test_a_save_the_keychain_did_not_keep_is_refused_and_taken_out(self) -> None:
        for mode in ('drops', 'mangles'):
            with self.subTest(mode=mode):
                system, security = mac_system()
                root = self.base / mode / 'keys'
                store = type(self.store())(root, system=system)
                security.mode = mode
                with self.assertRaises(KeyStoreError) as caught:
                    self.add(store)
                self.assertEqual(caught.exception.code, 'keychain_failed')
                self.assertEqual(security.items, {})
                self.assertEqual(store.list().keys, {})

    def test_a_locked_keychain_is_refused_and_never_answered_from_the_file(self) -> None:
        system, security = mac_system()
        store = self.store(system)
        self.add(store)
        security.mode = 'locked'
        with self.assertRaises(KeyRefused) as refused:
            store.credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'keychain_locked')
        with self.assertRaises(KeyStoreError) as caught:
            self.add(store, key_id='second', value=OTHER)
        self.assertEqual(caught.exception.code, 'keychain_locked')
        self.assertFalse((self.root / 'secrets.json').exists())

    def test_a_vanished_item_is_secret_missing_and_can_still_be_removed(self) -> None:
        system, security = mac_system()
        store = self.store(system)
        self.add(store)
        security.items.clear()
        with self.assertRaises(KeyRefused) as refused:
            store.credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'secret_missing')
        store.remove('codex-key')
        self.assertEqual(store.list().keys, {})

    def test_other_failures_are_keychain_failed_and_say_nothing_of_the_key(self) -> None:
        system, security = mac_system()
        store = self.store(system)
        security.mode = 'fails'
        with self.assertRaises(KeyStoreError) as caught:
            self.add(store)
        self.assertEqual(caught.exception.code, 'keychain_failed')
        self.assertNotIn(MARKER, str(caught.exception))
        self.assertNotIn(MARKER.encode().hex(), str(caught.exception))
