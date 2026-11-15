"""The Secret Service backend through real subprocesses, against a stand-in ``secret-tool`` on a PATH of its own.

The stand-in records each call's arguments, its whole environment and the SHA-256 of what it read on standard
input. The tests check that the key reached it on standard input only.
"""
from __future__ import annotations

import hashlib
import json
import socket
import tempfile
from pathlib import Path

from recursion_core.keys.errors import KeyRefused, KeyStoreError
from recursion_core.keys.store import KeyStore
from recursion_core.keys.system import System
from recursion_core.keys.tests.support import (
    MARKER,
    OTHER,
    TempStoreCase,
    fake_tools,
    needs_posix,
    tool_calls,
)


@needs_posix
class SecretToolTest(TempStoreCase):
    def setUp(self) -> None:
        super().setUp()
        tools = tempfile.TemporaryDirectory(prefix='keys-tools-')
        self.addCleanup(tools.cleanup)
        self.bin, self.tool_state = fake_tools(Path(tools.name))
        runtime = Path(tools.name) / 'run'
        runtime.mkdir(mode=0o700)
        self.sock = socket.socket(socket.AF_UNIX)     # a socket where a session bus would be; nothing listens
        self.addCleanup(self.sock.close)
        self.sock.bind(str(runtime / 'bus'))
        self.environ = {'PATH': str(self.bin), 'HOME': str(self.base), 'XDG_RUNTIME_DIR': str(runtime),
                        'ANTHROPIC_API_KEY': OTHER}

    def real_store(self) -> KeyStore:
        return KeyStore(self.root, system=System(self.environ, platform='linux'))

    def test_the_key_goes_to_secret_tool_on_stdin_only_and_comes_back_exactly(self) -> None:
        store = self.real_store()
        self.add(store)
        self.assertEqual(store.list().backend, 'secret_service')
        self.assertFalse((self.root / 'secrets.json').exists())
        self.assertEqual(store.credential('codex-key', 'CODEX_API_KEY'), MARKER)
        store.remove('codex-key')
        calls = tool_calls(self.tool_state)
        self.assertEqual([c['argv'][0] for c in calls], ['store', 'lookup', 'clear'])
        stored = calls[0]
        self.assertEqual(stored['stdin_sha256'], hashlib.sha256(MARKER.encode()).hexdigest())
        self.assertEqual(stored['argv'], ['store', '--label=Recursion API key codex-key', 'application', 'recursion',
                                          'recursion_key_id', 'codex-key'])
        for call in calls:
            self.assertNotIn(MARKER, json.dumps(call['argv']))
            self.assertNotIn(MARKER, json.dumps(call['env']))
            self.assertNotIn(OTHER, json.dumps(call['env']))       # the caller's own keys do not reach it either
            self.assertEqual(set(call['env']) - {'PWD', 'SHLVL', '_'},
                             {'PATH', 'HOME', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS', 'LC_ALL', 'LANG'})
        self.assertEqual(json.loads((self.tool_state / 'items.json').read_text()), {})

    def test_a_locked_keychain_is_refused_with_its_code(self) -> None:
        store = self.real_store()
        self.add(store)
        (self.tool_state / 'mode').write_text('locked')
        with self.assertRaises(KeyRefused) as refused:
            store.credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'keychain_locked')
        with self.assertRaises(KeyStoreError) as caught:
            self.add(store, key_id='second', value=OTHER)
        self.assertEqual(caught.exception.code, 'keychain_locked')
        self.assertIn('locked collection', caught.exception.detail)
        self.assertFalse((self.root / 'secrets.json').exists())

    def test_without_secret_tool_a_keychain_host_is_refused(self) -> None:
        (self.bin / 'secret-tool').unlink()
        with self.assertRaises(KeyStoreError) as caught:
            self.add(self.real_store())
        self.assertEqual(caught.exception.code, 'keychain_tool_missing')
        self.assertFalse(self.base.joinpath('config').exists())

    def test_an_item_that_is_not_a_saved_key_is_refused(self) -> None:
        store = self.real_store()
        self.add(store)
        items = json.loads((self.tool_state / 'items.json').read_text())
        items = {key: 'two words' for key in items}
        (self.tool_state / 'items.json').write_text(json.dumps(items))
        with self.assertRaises(KeyRefused) as refused:
            store.credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'value_invalid')
