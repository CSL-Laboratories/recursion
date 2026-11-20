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
from recursion_core.keys.tests.support import MARKER, TempStoreCase, needs_posix

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
        keys = self.store()
        self.add(keys)
        with mock.patch.object(store_mod, '_default', keys):
            self.assertEqual(hooks.credentials('codex-key', 'CODEX_API_KEY'), MARKER)
