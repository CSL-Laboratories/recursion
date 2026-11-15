"""Lane KEYS' registrant against SPINE's real hooks object (``control/hooks.py``), the shared fake.

``control/hooks.py`` arrives with SPINE's seams. On a tree without it (``master`` before the seams merge)
these tests are SKIPPED, by name, and the registrant is exercised only on a tree that has the seams (the lane
runs them on a local merge of its branch with SPINE's pinned head). A skip here is not a pass.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import unittest
from unittest import mock

from recursion_core.keys import store as store_mod
from recursion_core.keys.tests.support import MARKER, OTHER, TempStoreCase, needs_posix

HAS_SEAMS = importlib.util.find_spec('recursion_core.control.hooks') is not None
needs_seams = unittest.skipUnless(HAS_SEAMS, "SPINE's seams (control/hooks.py) are not on this tree yet")


@needs_posix
@needs_seams
class RegistrantTest(TempStoreCase):
    def hooks(self):
        from recursion_core.control import hooks as seams
        from recursion_core.keys import hooks as keys_hooks

        hooks = seams.Hooks()
        keys_hooks.register(hooks)
        hooks.freeze()
        return hooks

    def test_the_credential_source_is_registered_into_s7(self) -> None:
        keys = self.store()
        self.add(keys)
        hooks = self.hooks()
        with mock.patch.object(store_mod, '_default', keys):
            self.assertEqual(hooks.credentials('codex-key', 'CODEX_API_KEY'), MARKER)
            for conn, env in (('codex-key', 'OPENAI_API_KEY'), ('nobody', 'CODEX_API_KEY')):
                with self.assertRaises(KeyError) as caught:
                    hooks.credentials(conn, env)
                self.assertIs(type(caught.exception), KeyError)
                self.assertEqual(caught.exception.args, (conn,))

    def test_with_nothing_registered_s7_still_refuses(self) -> None:
        from recursion_core.control import hooks as seams

        bare = seams.bare()
        keys = self.store()
        self.add(keys)
        with mock.patch.object(store_mod, '_default', keys), self.assertRaises(KeyError):
            bare.credentials('codex-key', 'CODEX_API_KEY')

    def test_the_key_family_is_in_the_command_table(self) -> None:
        hooks = self.hooks()
        self.assertEqual(hooks.commands.names(), ('key',))
        top = argparse.ArgumentParser(prog='recursion-control')
        sub = top.add_subparsers(dest='command')
        hooks.commands.add_parsers(sub)
        args = top.parse_args(['key', 'list'])
        args.out, args.err = io.StringIO(), io.StringIO()
        from recursion_core.keys import cli
        with mock.patch.object(cli, 'KeyStore', lambda *a, **k: self.store()):
            self.assertEqual(hooks.commands.dispatch(args, client=None), 0)
        self.assertIn('No key is saved', args.out.getvalue())

    def test_registration_is_once_only(self) -> None:
        from recursion_core.control import hooks as seams
        from recursion_core.keys import hooks as keys_hooks

        hooks = seams.Hooks()
        keys_hooks.register(hooks)
        with self.assertRaises(seams.HookError):
            keys_hooks.register(hooks)

    def test_compose_loads_the_registrant(self) -> None:
        from recursion_core.control import hooks as seams

        hooks = seams.compose()
        self.assertIn('key', hooks.commands.names())
        self.assertIn('key.list', hooks.methods.registered())
        keys = self.store()
        self.add(keys)
        with mock.patch.object(store_mod, '_default', keys):
            self.assertEqual(hooks.credentials('codex-key', 'CODEX_API_KEY'), MARKER)


@needs_posix
@needs_seams
class KeyListTest(TempStoreCase):
    """``key.list`` through SPINE's real method table: registered, dispatched, and its answer's published shape."""

    def dispatch(self, params, keys=None):
        from recursion_core.control import hooks as seams
        from recursion_core.keys import hooks as keys_hooks

        hooks = seams.Hooks()
        keys_hooks.register(hooks)
        hooks.freeze()
        self.assertEqual(hooks.methods.registered(), ('key.list',))
        with mock.patch.object(store_mod, '_default', keys if keys is not None else self.store()):
            return hooks.methods.dispatch('key.list', params, seams.OWNER, lambda method, inner: None)

    def test_the_answer_is_the_published_shape_and_matches_the_fixture(self) -> None:
        import json
        import pathlib
        import re

        keys = self.store()
        self.add(keys)
        self.add(keys, key_id='claude-key', value=OTHER, env='ANTHROPIC_API_KEY', provider='anthropic', cap=0)
        answer = self.dispatch({}, keys)
        fixture = json.loads((pathlib.Path(__file__).parent / 'fixtures' / 'key.list.ok.json').read_text())
        self.assertEqual(answer, fixture)
        self.assertEqual(set(answer), {'code', 'keys'})
        token = re.compile(r'[A-Za-z0-9._:-]{1,128}')
        stamp = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z')
        for item in answer['keys']:
            self.assertEqual(set(item), {'key_id', 'provider', 'env_name', 'created_at', 'store'})
            self.assertTrue(token.fullmatch(item['key_id']))
            self.assertTrue(stamp.fullmatch(item['created_at']))
            self.assertIn(item['store'], ('secret_service', 'macos_keychain', 'windows_credential', 'owner_file'))
        self.assertNotIn(MARKER, json.dumps(answer))
        self.assertNotIn(OTHER, json.dumps(answer))

    def test_no_store_is_an_empty_list(self) -> None:
        self.assertEqual(self.dispatch({}), {'code': 'ok', 'keys': []})
        self.assertFalse(self.base.joinpath('config').exists())

    def test_params_are_refused_by_name(self) -> None:
        self.assertEqual(self.dispatch({'all': True}), {'code': 'request_invalid', 'detail': 'all'})

    def test_a_store_it_cannot_read_is_unavailable_with_the_stores_code(self) -> None:
        keys = self.store()
        self.add(keys)
        (self.root / 'index.json').chmod(0o644)
        self.assertEqual(self.dispatch({}, keys), {'code': 'unavailable', 'detail': 'file_invalid'})

    def test_it_never_asks_the_keychain(self) -> None:
        from recursion_core.keys.tests.support import keychain_system

        system, chain = keychain_system()
        keys = self.store(system)
        self.add(keys)
        system.calls.clear()
        chain.mode = 'locked'
        self.assertEqual(self.dispatch({}, keys)['keys'][0]['store'], 'secret_service')
        self.assertEqual(system.calls, [])

    def test_the_answer_is_each_records_published_fields_and_list_json_adds_only_spend(self) -> None:
        import json

        from recursion_core.keys.store import PUBLISHED, SHOWN

        keys = self.store()
        self.add(keys, cap=12.5)
        record = keys.list().keys['codex-key']
        item = self.dispatch({}, keys)['keys'][0]
        self.assertEqual(item, record.published())
        self.assertEqual(tuple(item), PUBLISHED)
        local = record.as_json()                          # the local key list --json, round 6's I1
        self.assertEqual(tuple(local), SHOWN)
        self.assertEqual({name: local[name] for name in PUBLISHED}, item)
        self.assertEqual(set(local) - set(item), {'metered_spend_allowed', 'cap_usd'})
        self.assertNotIn(record.save_id, json.dumps(item) + json.dumps(local))
