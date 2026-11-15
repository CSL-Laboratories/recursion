"""The store: what is saved where, pinning, a locked keychain, validation and the index's exact shape."""
from __future__ import annotations

import json
import os
from unittest import mock

from recursion_core.keys.errors import KeyRefused, KeyStoreError
from recursion_core.keys.store import KeyStore
from recursion_core.keys.tests.support import (
    MARKER,
    OTHER,
    FakeSystem,
    TempStoreCase,
    busctl,
    file_system,
    keychain_system,
    needs_posix,
)


@needs_posix
class SaveTest(TempStoreCase):
    def test_the_index_holds_no_value_and_the_owner_file_holds_it(self) -> None:
        store = self.store()
        record = self.add(store)
        self.assertEqual((record.key_id, record.provider, record.env_name, record.store),
                         ('codex-key', 'openai', 'CODEX_API_KEY', 'owner_file'))
        self.assertEqual(record.created_at, '2025-11-14T22:40:00.250Z')
        index = json.loads((self.root / 'index.json').read_text())
        self.assertRegex(index['store_id'], r'^[0-9a-f]{32}$')
        self.assertRegex(record.save_id, r'^[0-9a-f]{32}$')
        self.assertEqual(index, {'version': 1, 'store_id': index['store_id'], 'backend': 'owner_file', 'keys': {
            'codex-key': {'provider': 'openai', 'env_name': 'CODEX_API_KEY', 'created_at': '2025-11-14T22:40:00.250Z',
                          'store': 'owner_file', 'metered_spend_allowed': True, 'cap_usd': 5,
                          'save_id': record.save_id}}, 'pending': {}})
        self.assertNotIn(MARKER.encode(), (self.root / 'index.json').read_bytes())
        self.assertEqual(json.loads((self.root / 'secrets.json').read_text()),
                         {'version': 1, 'secrets': {'codex-key': MARKER}})

    def test_a_keychain_host_keeps_the_value_in_the_keychain_and_writes_no_owner_file(self) -> None:
        system, chain = keychain_system()
        store = self.store(system)
        self.add(store)
        self.assertEqual(store.list().backend, 'secret_service')
        self.assertFalse((self.root / 'secrets.json').exists())
        self.assertEqual(list(chain.items.values()), [MARKER.encode()])
        self.assertEqual(store.credential('codex-key', 'CODEX_API_KEY'), MARKER)
        store.remove('codex-key')
        self.assertEqual(chain.items, {})
        self.assertEqual(store.list().keys, {})

    def test_a_saved_id_is_refused_until_it_is_removed(self) -> None:
        store = self.store()
        self.add(store)
        with self.assertRaises(KeyStoreError) as caught:
            self.add(store, value=OTHER)
        self.assertEqual(caught.exception.code, 'key_exists')
        self.assertEqual(store.credential('codex-key', 'CODEX_API_KEY'), MARKER)
        store.remove('codex-key')
        self.add(store, value=OTHER)
        self.assertEqual(store.credential('codex-key', 'CODEX_API_KEY'), OTHER)

    def test_removing_one_key_keeps_the_others(self) -> None:
        store = self.store()
        self.add(store)
        self.add(store, key_id='claude-key', value=OTHER, env='ANTHROPIC_API_KEY', provider='anthropic')
        store.remove('codex-key')
        self.assertEqual(sorted(store.list().keys), ['claude-key'])
        self.assertEqual(json.loads((self.root / 'secrets.json').read_text())['secrets'], {'claude-key': OTHER})

    def test_an_index_write_that_fails_takes_the_value_out_again(self) -> None:
        system, chain = keychain_system()
        store = self.store(system)
        self.add(store)
        with mock.patch.object(KeyStore, '_write_index', side_effect=OSError(28, 'No space left on device')), \
                self.assertRaises(OSError):
            self.add(store, key_id='second', value=OTHER)
        self.assertEqual(list(chain.items.values()), [MARKER.encode()])
        self.assertEqual(sorted(store.list().keys), ['codex-key'])


@needs_posix
class PinTest(TempStoreCase):
    def test_a_keychain_that_stops_answering_is_refused_and_the_file_never_used(self) -> None:
        system, _ = keychain_system()
        self.add(self.store(system))
        later = self.store(file_system())            # no session bus any more
        with self.assertRaises(KeyRefused) as refused:
            later.credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'keychain_unavailable')
        for call in (lambda: self.add(later, key_id='second', value=OTHER), lambda: later.remove('codex-key')):
            with self.assertRaises(KeyStoreError) as caught:
                call()
            self.assertEqual(caught.exception.code, 'keychain_unavailable')
        self.assertFalse((self.root / 'secrets.json').exists())
        self.assertEqual(sorted(later.list().keys), ['codex-key'])     # list touches no backend

    def test_an_owner_file_store_refuses_once_a_keychain_appears(self) -> None:
        self.add(self.store())
        system, chain = keychain_system()
        later = self.store(system)
        with self.assertRaises(KeyRefused) as refused:
            later.credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'backend_changed')
        with self.assertRaises(KeyStoreError) as caught:
            self.add(later, key_id='second', value=OTHER)
        self.assertEqual(caught.exception.code, 'backend_changed')
        self.assertEqual(chain.items, {})
        self.assertEqual(self.store().credential('codex-key', 'CODEX_API_KEY'), MARKER)   # the pinned file still reads

    def test_a_host_that_cannot_say_refuses_rather_than_writes_a_file(self) -> None:
        for system, code in ((FakeSystem(tools={'busctl': busctl(owner=True)}, bus='unix:path=/b'), 'keychain_tool_missing'),
                             (FakeSystem(bus='unix:path=/b'), 'keychain_undetermined'),
                             (FakeSystem(platform='darwin'), 'backend_not_built'),
                             (FakeSystem(platform='win32'), 'backend_not_built')):
            with self.subTest(code=code, platform=system.platform):
                with self.assertRaises(KeyStoreError) as caught:
                    self.add(self.store(system))
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(self.base.joinpath('config').exists())     # refused before any folder was made

    def test_the_pin_is_released_once_no_key_is_left(self) -> None:
        store = self.store()
        self.add(store)
        store.remove('codex-key')
        self.assertIsNone(json.loads((self.root / 'index.json').read_text())['backend'])
        system, _ = keychain_system()
        self.add(self.store(system), value=OTHER)
        self.assertEqual(self.store(system).list().backend, 'secret_service')
        self.assertEqual(json.loads((self.root / 'secrets.json').read_text())['secrets'], {})

    def test_a_locked_keychain_is_refused_and_never_answered_from_the_file(self) -> None:
        for lock in ('locked', 'dismissed', 'fail'):
            with self.subTest(mode=lock):
                system, chain = keychain_system()
                root = self.base / lock / 'keys'
                store = KeyStore(root, system=system)
                chain.mode = lock
                with self.assertRaises(KeyStoreError) as caught:
                    self.add(store)
                expected = 'keychain_failed' if lock == 'fail' else 'keychain_locked'
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(store.list().keys, {})
                chain.mode = 'ok'
                self.add(store)
                chain.mode = lock
                with self.assertRaises(KeyRefused) as refused:
                    store.credential('codex-key', 'CODEX_API_KEY')
                self.assertEqual(refused.exception.code, expected)
                self.assertFalse((root / 'secrets.json').exists())

    def test_a_keychain_item_that_vanished_is_refused(self) -> None:
        system, chain = keychain_system()
        store = self.store(system)
        self.add(store)
        chain.items.clear()
        with self.assertRaises(KeyRefused) as refused:
            store.credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'secret_missing')
        store.remove('codex-key')                       # and it can still be removed
        self.assertEqual(store.list().keys, {})


@needs_posix
class ValidationTest(TempStoreCase):
    def refused(self, code, **kw) -> None:
        args = {'key_id': 'codex-key', 'provider': 'openai', 'env_name': 'CODEX_API_KEY', 'value': MARKER,
                'metered_spend_allowed': True, 'cap_usd': 5}
        args.update(kw)
        store = self.store()
        with self.assertRaises(KeyStoreError) as caught:
            store.add(args.pop('key_id'), args.pop('provider'), args.pop('env_name'), args.pop('value'), **args)
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(MARKER, str(caught.exception))
        self.assertFalse(self.base.joinpath('config').exists())

    def test_ids(self) -> None:
        for bad in ('', '-x', '.hidden', 'a b', 'a\nb', 'key\n', 'a/b', 'x' * 129, 'k\u00e9y', 7, None):
            with self.subTest(key_id=bad):
                self.refused('id_invalid', key_id=bad)

    def test_providers_and_variable_names(self) -> None:
        for bad in ('', 'OpenAI', 'Anthropic', 'openai\n', ' openai', '-x', 'a' * 65, 'open ai', 7, None):
            with self.subTest(provider=bad):
                self.refused('provider_invalid', provider=bad)
        for bad in ('', 'codex_api_key', '1KEY', 'KEY-NAME', 'KEY\n', 'A' * 129):
            with self.subTest(env=bad):
                self.refused('env_name_invalid', env_name=bad)

    def test_every_published_provider_id_is_accepted(self) -> None:
        from recursion_core.keys import validate

        self.assertEqual(validate.PROVIDER_IDS, ('anthropic', 'openai', 'google', 'xai', 'cursor', 'opencode', 'nous',
                                                 'refuseless'))
        self.assertTrue(all(why.startswith('CONTRACT ') for _, why in validate.PROVIDERS))
        store = self.store()
        for number, provider in enumerate(validate.PROVIDER_IDS):
            with self.subTest(provider=provider):
                record = self.add(store, key_id=f'key-{number}', provider=provider)
                self.assertEqual(store.list().keys[record.key_id].provider, provider)

    def test_any_vendor_can_be_saved_and_only_published_ids_are_known(self) -> None:
        # "API keys are configurable for every vendor" (CONTRACT 275); ROUTING routes the published ids only.
        from recursion_core.keys import validate

        store = self.store()
        for number, provider in enumerate(('mistral', 'deepseek', 'acme.ai', 'z-ai')):
            with self.subTest(provider=provider):
                record = self.add(store, key_id=f'other-{number}', provider=provider)
                self.assertEqual(store.list().keys[record.key_id].provider, provider)
                self.assertFalse(validate.known_provider(provider))
                # Kept and listed, and handed to no engine (the Liaison's 6:03 PM ruling on K14).
                with self.assertRaises(KeyRefused) as refused:
                    store.credential(record.key_id, 'CODEX_API_KEY')
                self.assertEqual(refused.exception.code, 'provider_unrouted')
        self.assertTrue(validate.known_provider('anthropic'))

    def test_a_key_typed_in_the_ids_place_is_refused_and_not_repeated(self) -> None:
        for bad in (MARKER.replace('-', '') + 'X9', 'sk-proj-Ab3dEf5hIj7kLm9nOp1qRs', 'x' * 65):
            with self.subTest(key_id=bad[:12]):
                store = self.store()
                with self.assertRaises(KeyStoreError) as caught:
                    store.add(bad, 'openai', 'CODEX_API_KEY', OTHER, metered_spend_allowed=True, cap_usd=5)
                self.assertEqual(caught.exception.code, 'id_invalid')
                self.assertNotIn(bad, str(caught.exception))
                with self.assertRaises(KeyStoreError) as removed:
                    store.remove(bad)
                self.assertEqual(removed.exception.code, 'id_invalid')
                self.assertNotIn(bad, str(removed.exception))
        self.add(self.store(), key_id='codex-key-2026-11')       # an ordinary id with digits is fine

    def test_values(self) -> None:
        for bad in ('short', 'has space inside it', 'tab\tinside-it', 'newline\ninside', 'nul\x00inside-it',
                    'caf\u00e9-not-ascii', 'x' * 4097, MARKER + '\n', b'bytes-not-str', None):
            with self.subTest(value=repr(bad)[:20]):
                self.refused('value_invalid', value=bad)

    def test_spend_opt_in_and_cap_agree(self) -> None:
        for allowed, cap in ((True, 0), (False, 5), (True, -1), (True, 100001), (True, 1.005), (True, float('nan')),
                             (True, True), ('yes', 5), (1, 5), (True, '5')):
            with self.subTest(allowed=allowed, cap=cap):
                self.refused('cap_invalid', metered_spend_allowed=allowed, cap_usd=cap)


@needs_posix
class IndexShapeTest(TempStoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.add(self.store())
        self.good = json.loads((self.root / 'index.json').read_text())

    def write(self, text: str) -> None:
        path = self.root / 'index.json'
        os.unlink(path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as fh:
            fh.write(text)

    def entry(self, **change) -> str:
        doc = json.loads(json.dumps(self.good))
        doc['keys']['codex-key'].update(change)
        return json.dumps(doc)

    def test_anything_but_the_exact_shape_is_refused(self) -> None:
        good = json.dumps(self.good)
        cases = {
            'version true': good.replace('"version": 1', '"version": true'),
            'version 2': good.replace('"version": 1', '"version": 2'),
            'duplicate field': good.replace('"version": 1', '"version": 1, "version": 1'),
            'extra field': good.replace('"version": 1', '"version": 1, "x": 0'),
            'unknown backend': good.replace('"backend": "owner_file"', '"backend": "kwallet"'),
            'no backend with keys': good.replace('"backend": "owner_file"', '"backend": null'),
            'newline in id': good.replace('"codex-key"', '"codex\\nkey"'),
            'store differs': self.entry(store='secret_service'),
            'opt-in without cap': self.entry(cap_usd=0),
            'cap without opt-in': self.entry(metered_spend_allowed=False),
            'opt-in as 1': self.entry(metered_spend_allowed=1),
            'cap NaN': good.replace('"cap_usd": 5', '"cap_usd": NaN'),
            'cap as text': self.entry(cap_usd='5'),
            'bad timestamp': self.entry(created_at='2025-11-14 22:40:00Z'),
            'bad variable': self.entry(env_name='codex_api_key'),
            'malformed provider': self.entry(provider='Acme'),
            'backend and store both unknown': good.replace('"owner_file"', '"kwallet"'),
            'no such day': self.entry(created_at='2025-02-31T22:40:00.250Z'),
            'year zero': self.entry(created_at='0000-11-14T22:40:00.250Z'),
            'cap far too large': self.entry(cap_usd=10 ** 500),
            'no pending field': json.dumps({k: v for k, v in self.good.items() if k != 'pending'}),
            'pending with another op': json.dumps({**self.good, 'pending': {'x': {'op': 'move',
                                                                                  'started_at': '2025-11-14T22:40:00.250Z'}}}),
            'pending with no time': json.dumps({**self.good, 'pending': {'x': {'op': 'add'}}}),
            'pending under a bad id': json.dumps({**self.good, 'pending': {'-x': {'op': 'add',
                                                                                    'started_at': '2025-11-14T22:40:00.250Z'}}}),
            'pending with no backend': json.dumps({'version': 1, 'backend': None, 'keys': {}, 'pending': {
                'x': {'op': 'add', 'started_at': '2025-11-14T22:40:00.250Z'}}}),
            'missing field': json.dumps({**self.good, 'keys': {'codex-key': {'provider': 'openai'}}}),
            'not JSON': '{"version": 1,',
            'not ASCII': good.replace('openai', 'open\u00e4i'),
            'a list': '[]',
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                self.write(text)
                with self.assertRaises(KeyRefused) as refused:
                    self.store().credential('codex-key', 'CODEX_API_KEY')
                self.assertEqual(refused.exception.code, 'index_invalid')
                with self.assertRaises(KeyStoreError) as caught:
                    self.store().list()
                self.assertEqual(caught.exception.code, 'index_invalid')
        self.write(good)
        self.assertEqual(self.store().credential('codex-key', 'CODEX_API_KEY'), MARKER)

    def test_an_owner_file_of_another_shape_is_refused_without_its_text(self) -> None:
        path = self.root / 'secrets.json'
        for text in ('{"version": 1, "secrets": {"codex-key": "' + MARKER + '"}, "x": 1}',
                     '{"version": 1, "secrets": {"codex-key": "' + MARKER + ' "}}',
                     '{"version": 1, "secrets": {"codex-key": "' + MARKER + '"', 'not json ' + MARKER):
            with self.subTest(text=text[:30]):
                os.unlink(path)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w') as fh:
                    fh.write(text)
                with self.assertRaises(KeyStoreError) as caught:
                    self.store().values()
                self.assertEqual(caught.exception.code, 'owner_file_invalid')
                for exc in (caught.exception, caught.exception.__context__, caught.exception.__cause__):
                    self.assertNotIn(MARKER, repr(exc))
                self.assertIsNone(caught.exception.__context__)
