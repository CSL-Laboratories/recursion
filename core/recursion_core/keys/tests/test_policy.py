"""``spend_policy()`` for lane QUOTA and ``describe()`` for lane INSTALL's doctor: index only, never a value."""
from __future__ import annotations

import json
import os

from recursion_core.keys.errors import KeyRefused
from recursion_core.keys.tests.support import (
    MARKER,
    OTHER,
    FakeSystem,
    TempStoreCase,
    busctl,
    keychain_system,
    needs_posix,
)


@needs_posix
class SpendPolicyTest(TempStoreCase):
    def test_the_opt_in_and_cap_as_typed_at_add(self) -> None:
        store = self.store()
        self.add(store, cap=12.5)
        self.add(store, key_id='claude-key', value=OTHER, env='ANTHROPIC_API_KEY', provider='anthropic', cap=0)
        self.assertEqual(store.spend_policy('codex-key'), {'metered_spend_allowed': True, 'cap_usd': 12.5})
        self.assertEqual(store.spend_policy('claude-key'), {'metered_spend_allowed': False, 'cap_usd': 0})

    def test_unknown_ids_are_a_plain_key_error(self) -> None:
        store = self.store()
        for conn in ('nobody', 'bad id', ''):
            with self.subTest(conn=conn), self.assertRaises(KeyError) as caught:
                store.spend_policy(conn)
            self.assertIs(type(caught.exception), KeyError)
            self.assertEqual(caught.exception.args, (conn,))

    def test_it_reads_the_index_only(self) -> None:
        system, chain = keychain_system()
        store = self.store(system)
        self.add(store)
        system.calls.clear()
        chain.mode = 'locked'                 # a keychain that would refuse is never asked
        self.assertEqual(store.spend_policy('codex-key'), {'metered_spend_allowed': True, 'cap_usd': 5})
        self.assertEqual(system.calls, [])

    def test_an_index_it_cannot_read_is_a_typed_refusal(self) -> None:
        store = self.store()
        self.add(store)
        (self.root / 'index.json').chmod(0o644)
        with self.assertRaises(KeyRefused) as refused:
            store.spend_policy('codex-key')
        self.assertEqual((refused.exception.code, refused.exception.args), ('file_invalid', ('codex-key',)))
        self.assertIsNone(refused.exception.__context__)


@needs_posix
class DescribeTest(TempStoreCase):
    def test_nothing_saved_on_a_file_host(self) -> None:
        self.assertEqual(self.store().describe(), {
            'folder': str(self.root), 'store': None, 'keys': 0, 'owner_file': str(self.root / 'secrets.json'),
            'refusal': None, 'offered': 'owner_file', 'offered_refusal': None})
        self.assertFalse(self.base.joinpath('config').exists())        # describing creates nothing

    def test_a_keychain_store_names_its_backend_and_no_file(self) -> None:
        system, _ = keychain_system()
        store = self.store(system)
        self.add(store)
        self.add(store, key_id='claude-key', value=OTHER, env='ANTHROPIC_API_KEY', provider='anthropic')
        described = store.describe()
        self.assertEqual((described['store'], described['offered'], described['keys'], described['owner_file']),
                         ('secret_service', 'secret_service', 2, None))
        self.assertNotIn(MARKER, json.dumps(described))
        self.assertNotIn(OTHER, json.dumps(described))

    def test_it_reports_what_it_cannot_use_and_does_not_raise(self) -> None:
        store = self.store()
        self.add(store)
        os.chmod(self.root, 0o755)
        try:
            later = self.store(FakeSystem(tools={'busctl': busctl(owner=True)}, bus='unix:path=/b'))
            described = later.describe()
        finally:
            os.chmod(self.root, 0o700)
        self.assertEqual(described['refusal']['code'], 'folder_invalid')
        self.assertEqual(described['offered_refusal']['code'], 'keychain_tool_missing')
        self.assertIsNone(described['store'])
        self.assertIsNone(described['offered'])

    def test_a_pinned_file_store_on_a_host_that_now_has_a_keychain(self) -> None:
        self.add(self.store())
        system, _ = keychain_system()
        described = self.store(system).describe()
        self.assertEqual((described['store'], described['offered'], described['owner_file']),
                         ('owner_file', 'secret_service', str(self.root / 'secrets.json')))
