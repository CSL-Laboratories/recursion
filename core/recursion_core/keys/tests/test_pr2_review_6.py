"""The sixth read of pull request 2 (#422): DeepSeek V4.1 Flash's F1 and F2. Every key is made up; the macOS tests use
the stand-in ``security`` of ``test_macos.py``."""
from __future__ import annotations

import io

from recursion_core.keys import __main__ as entry
from recursion_core.keys.backends import MacKeychain
from recursion_core.keys.errors import KeyStoreError
from recursion_core.keys.store import KeyStore
from recursion_core.keys.system import Completed
from recursion_core.keys.tests.support import FakeSystem, needs_posix
from recursion_core.keys.tests.test_macos import (
    ELSEWHERE,
    LOGIN,
    FakeSecurity,
    fixed_clock,
)
from recursion_core.keys.tests.test_pr2_review_3 import MacCase
from recursion_core.keys.tests.test_pr2_review_5 import ADVICE

DENIED = b'security: SecKeychainItemDelete: User interaction is not allowed.\n'


class DeleteDenied(FakeSecurity):
    """The stand-in ``security`` with one more failure: a keychain that lists its items and refuses to delete one (an
    item whose access control would ask), exit 36, as ``security`` answers when it may not ask."""

    deny = False

    def _answer(self, argv, input):
        if self.deny and argv[1] == 'delete-generic-password':
            return Completed(36, b'', DENIED)
        return super()._answer(argv, input)


@needs_posix
class AuditForetellsNoRemovalTest(MacCase):
    """DeepSeek r6 F1 and F2: what ``key audit`` prints about an unfinished operation is what ``key remove`` then does.
    A keychain that answers the audit's count can refuse the removal (F1), and one that refuses the count can let the
    removal through (F2), so the audit says only that the removal clears the value or names the refusal that stops
    it; the count's own refusal is a line of its own."""

    def mac(self, name='keys'):
        security = DeleteDenied()
        system = FakeSystem(platform='darwin', tools={'security': security})
        return KeyStore(self.base / name / 'keys', system=system, clock=fixed_clock), system, security

    def unfinished(self, store, security) -> None:
        held = len(security.items)
        security.login = ELSEWHERE
        with self.assertRaises(KeyStoreError):
            store.remove('codex-key')                       # refused before any delete: its intent stays
        security.login = LOGIN
        index = store.list()
        self.assertEqual((sorted(index.keys), sorted(index.pending), len(security.items)),
                         (['codex-key'], ['codex-key'], held))

    def audit(self, store) -> tuple:
        out = io.StringIO()
        status = entry.main(['audit', '--no-journal'], out=out, err=io.StringIO(), store=store)
        return status, out.getvalue()

    def test_a_count_that_answers_beside_a_removal_that_refuses(self) -> None:
        for shape, code in (('denied', 'keychain_locked'), ('stuck', 'keychain_failed'), ('many', 'keychain_failed')):
            with self.subTest(shape=shape):
                store, _, security = self.mac(shape)
                self.add(store)
                if shape == 'many':                         # more items of the id than one removal deletes
                    first = security.items[0]
                    for n in range(MacKeychain.DELETE_LIMIT + 6):
                        security.items.append(dict(first, svce=f'recursion.{first["gena"]}.{n:032x}.openai'))
                self.unfinished(store, security)
                if shape == 'denied':
                    security.deny = True
                elif shape == 'stuck':
                    security.mode = 'stuck'                 # the delete answers 0 and the item stays
                status, text = self.audit(store)
                self.assertEqual(status, 4)
                self.assertIn(ADVICE, text)
                self.assertNotIn('its items were not counted', text)      # the count answered
                with self.assertRaises(KeyStoreError) as caught:
                    store.remove('codex-key')
                self.assertEqual(caught.exception.code, code)               # the refusal the advice says it names
                index = store.list()
                self.assertEqual((sorted(index.keys), sorted(index.pending)), (['codex-key'], ['codex-key']))
                security.deny, security.mode = False, 'ok'
                store.remove('codex-key')                   # where the store allows it, the removal clears it
                self.assertEqual((store.list().keys, store.list().pending), ({}, {}))
                self.assertEqual([item for item in security.items if item['acct'] == 'codex-key'], [])

    def test_a_count_that_is_refused_beside_a_removal_that_works(self) -> None:
        store, _, security = self.mac('partial')
        self.add(store)
        self.unfinished(store, security)
        security.mode = 'dump_partial'                      # the listing stops early on an item; a delete needs none
        status, text = self.audit(store)
        self.assertEqual(status, 4)
        self.assertIn(ADVICE, text)
        self.assertIn('keychain: its items were not counted (keychain_failed)', text)
        self.assertNotIn('cannot clear', text)
        self.assertEqual(store.remove('codex-key').key_id, 'codex-key')
        self.assertEqual((store.list().keys, store.list().pending, security.items), ({}, {}, []))
