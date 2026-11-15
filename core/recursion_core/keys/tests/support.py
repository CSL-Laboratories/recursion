"""Test support for lane KEYS. Every key here is made up; no test reaches a real keychain, bus or terminal.

``FakeSystem`` answers PATH lookups and helper runs from a table of Python functions, so detection and
backends are driven with no program started. ``fake_tools`` writes stand-in ``secret-tool`` and ``busctl``
programs into a folder of their own, for the tests that go through real subprocesses: each records its
arguments, its environment and the SHA-256 of its standard input (never the input) in a log, and the stand-in
keychain keeps its items in a JSON file beside it. Those tests give the store a ``PATH`` holding only that
folder, so no real ``secret-tool`` can be found.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from recursion_core.keys import cli
from recursion_core.keys.store import KeyStore
from recursion_core.keys.system import Completed, System, ToolMissing

POSIX = os.name == 'posix'
LINUX = sys.platform.startswith('linux')
needs_posix = unittest.skipUnless(POSIX, 'the key store is POSIX-only in this slice (Windows backend: PR 3)')

# Made-up keys. Their shapes only resemble a vendor's; none was ever issued.
MARKER = 'madeup-KEYS-marker-7f3a9c2e1b-not-a-real-key'
OTHER = 'madeup-KEYS-second-0d4b-not-a-real-key'


class FakeSystem(System):
    """A ``System`` whose tools are Python functions: ``tools[name](argv, input) -> Completed``."""

    def __init__(self, environ=None, *, platform='linux', tools=None, bus=None) -> None:
        super().__init__(dict(environ or {'PATH': '/fake/bin', 'HOME': '/fake/home'}), platform=platform)
        self.tools = dict(tools or {})
        self.bus = bus
        self.calls = []

    def session_bus(self):
        return self.bus

    def which(self, name):
        return f'/fake/bin/{name}' if name in self.tools else None

    def run(self, argv, *, input=None, timeout):
        self.calls.append((list(argv), input))
        name = os.path.basename(argv[0])
        if name not in self.tools:
            raise ToolMissing(name)
        return self.tools[name](list(argv), input)

    def stream(self, argv):
        name = os.path.basename(argv[0])
        if name not in self.tools:
            raise ToolMissing(name)
        done = self.tools[name](list(argv), None)
        yield done.stdout


def busctl(owner: bool = False, activatable: bool = False, kwallet: bool = False):
    """A stand-in ``busctl``: ``ListNames`` and ``ListActivatableNames`` as ``busctl --user call`` prints them."""
    def run(argv, input):
        names = ['org.freedesktop.DBus']
        if ('ListNames' in argv and owner) or ('ListActivatableNames' in argv and activatable):
            names.append('org.freedesktop.secrets')
        if 'ListNames' in argv and kwallet:
            names.append('org.kde.kwalletd6')
        text = f'as {len(names)} ' + ' '.join(f'"{name}"' for name in names) + '\n'
        return Completed(0, text.encode(), b'')
    return run


class FakeKeychain:
    """An in-memory Secret Service behind a fake ``secret-tool``, for FakeSystem."""

    def __init__(self) -> None:
        self.items: dict = {}
        self.mode = 'ok'     # ok | locked | fail | dismissed

    def __call__(self, argv, input):
        action = argv[1]
        attrs = argv[2:] if action != 'store' else argv[3:]
        key = tuple(attrs)
        if self.mode == 'locked':
            return Completed(1, b'', b'secret-tool: Cannot create an item in a locked collection\n')
        if self.mode == 'dismissed':
            return Completed(1, b'', b'secret-tool: Prompt dismissed\n')
        if self.mode == 'fail':
            return Completed(1, b'', b'secret-tool: The name org.freedesktop.secrets was not provided\n')
        if action == 'store':
            self.items[key] = input
            return Completed(0, b'', b'')
        if action == 'lookup':
            if key in self.items:
                return Completed(0, self.items[key], b'')
            return Completed(1, b'', b'')
        if action == 'clear':
            # As libsecret: clear reports whether it removed something; secret-tool exits 1, silently, if not.
            return Completed(0, b'', b'') if self.items.pop(key, None) is not None else Completed(1, b'', b'')
        return Completed(2, b'', b'secret-tool: unknown action\n')


def keychain_system(**kw):
    """A FakeSystem with a session bus, a Secret Service on it, and an in-memory keychain."""
    chain = FakeKeychain()
    system = FakeSystem(tools={'busctl': busctl(owner=True), 'secret-tool': chain}, bus='unix:path=/fake/bus', **kw)
    return system, chain


def file_system(**kw):
    """A FakeSystem with no session bus: the host offers the owner-only file."""
    return FakeSystem(**kw)


class TempStoreCase(unittest.TestCase):
    """A fresh temp folder per test; ``self.root`` is where the store's folder will be."""

    def setUp(self) -> None:
        # No test in this process may reach the real terminal: a test that wants one hands in its own.
        no_terminal = mock.patch.object(cli.Terminal, 'open', return_value=None)
        no_terminal.start()
        self.addCleanup(no_terminal.stop)
        self._tmp = tempfile.TemporaryDirectory(prefix='keys-test-')
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.root = self.base / 'config' / 'recursion' / 'keys'

    def store(self, system=None, **kw) -> KeyStore:
        return KeyStore(self.root, system=system or file_system(), clock=lambda: 1_763_160_000.25, **kw)

    def add(self, store, key_id='codex-key', value=MARKER, env='CODEX_API_KEY', provider='openai', cap=5):
        return store.add(key_id, provider, env, value, metered_spend_allowed=cap > 0, cap_usd=cap)

    def state_bytes(self, *, skip=('secrets.json',)) -> bytes:
        """Every file under the temp folder (but the owner-only file) as bytes, for marker checks."""
        out = []
        for folder, _, files in os.walk(self.base):
            for name in files:
                if name in skip:
                    continue
                with open(os.path.join(folder, name), 'rb') as fh:
                    out.append(fh.read())
        return b'\n'.join(out)


# --------------------------------------------------------------------------- stand-in programs on disk
_SECRET_TOOL = r'''#!{python} -I
import hashlib, json, os, sys
STATE = {state!r}
argv = sys.argv[1:]
data = sys.stdin.buffer.read() if argv and argv[0] == 'store' else b''
with open(os.path.join(STATE, 'calls.jsonl'), 'a') as log:
    log.write(json.dumps({{'argv': argv, 'env': dict(os.environ), 'stdin_sha256': hashlib.sha256(data).hexdigest(),
                          'stdin_len': len(data)}}) + '\n')
mode = open(os.path.join(STATE, 'mode')).read().strip() if os.path.exists(os.path.join(STATE, 'mode')) else 'ok'
if mode == 'locked':
    sys.stderr.write('secret-tool: Cannot create an item in a locked collection\n'); sys.exit(1)
items_path = os.path.join(STATE, 'items.json')
items = json.load(open(items_path)) if os.path.exists(items_path) else {{}}
action, rest = argv[0], argv[1:]
if action == 'store':
    rest = [a for a in rest if not a.startswith('--label=')]
key = '\x1f'.join(rest)
if action == 'store':
    items[key] = data.decode('ascii')
elif action == 'lookup':
    if key not in items:
        sys.exit(1)
    sys.stdout.write(items[key]); sys.stdout.flush(); sys.exit(0)
elif action == 'clear':
    if items.pop(key, None) is None:
        sys.exit(1)
else:
    sys.stderr.write('secret-tool: unknown\n'); sys.exit(2)
with open(items_path, 'w') as fh:
    json.dump(items, fh)
'''

_BUSCTL = r'''#!{python} -I
import sys
if 'ListNames' in sys.argv:
    print('as 2 "org.freedesktop.DBus" "org.freedesktop.secrets"')
else:
    print('as 1 "org.freedesktop.DBus"')
'''


def fake_tools(folder: Path, *, secret_tool=True, busctl_tool=True) -> tuple:
    """Write stand-in programs into ``folder/bin``; returns (bin folder, state folder)."""
    bin_dir, state = folder / 'bin', folder / 'tool-state'
    bin_dir.mkdir(parents=True)
    state.mkdir()
    programs = {}
    if secret_tool:
        programs['secret-tool'] = _SECRET_TOOL.format(python=sys.executable, state=str(state))
    if busctl_tool:
        programs['busctl'] = _BUSCTL.format(python=sys.executable)
    for name, text in programs.items():
        path = bin_dir / name
        path.write_text(text)
        path.chmod(0o700)
    return bin_dir, state


def tool_calls(state: Path) -> list:
    path = state / 'calls.jsonl'
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
