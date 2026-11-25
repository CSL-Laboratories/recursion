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


LOGIN = '/org/freedesktop/secrets/collection/login'
LOGIN_CREATED = 1_700_000_000     # a made-up creation time, before the tests' clock (TempStoreCase.store)


def search_items_reply(found, locked) -> bytes:
    """The Secret Service's ``SearchItems`` reply as ``busctl --user call`` prints it: the signature, then the unlocked
    items' paths and the locked items' paths, each array as its length and its quoted paths. Paths only: no secret."""
    words, number = ['aoao'], 0
    for group in ([key for key in found if key not in locked], [key for key in found if key in locked]):
        words.append(str(len(group)))
        for _ in group:
            number += 1
            words.append(f'"{LOGIN}/{number}"')
    return (' '.join(words) + '\n').encode()


def busctl(owner: bool = False, activatable: bool = False, kwallet: bool = False, chain=None):
    """A stand-in ``busctl``: ``ListNames`` and ``ListActivatableNames`` as ``busctl --user call`` prints them; with a
    ``chain``, also the Secret Service's ``ReadAlias default`` and its collection's ``Created`` (``chain.collection``,
    None for a service that does not say), and its ``SearchItems`` over ``chain``'s items (locked ones too; a chain in
    mode ``fail`` is a service that is not there)."""
    def run(argv, input):
        if chain is not None and 'SearchItems' in argv:
            if chain.mode == 'fail':
                return Completed(1, b'', b'Call failed: The name org.freedesktop.secrets was not provided\n')
            return Completed(0, search_items_reply(chain._matches(argv[argv.index('a{ss}') + 2:]), chain.locked), b'')
        if chain is not None and ('ReadAlias' in argv or 'get-property' in argv):
            found = chain.collection
            if found is None:
                return Completed(1, b'', b'Call failed: No such method\n')
            if 'ReadAlias' in argv:
                return Completed(0, f'o "{found[0]}"\n'.encode(), b'')
            if found[0] in argv and 'Created' in argv:
                return Completed(0, f't {found[1]}\n'.encode(), b'')
            return Completed(1, b'', b'Failed to get property: Unknown object\n')
        names = ['org.freedesktop.DBus']
        if ('ListNames' in argv and owner) or ('ListActivatableNames' in argv and activatable):
            names.append('org.freedesktop.secrets')
        if 'ListNames' in argv and kwallet:
            names.append('org.kde.kwalletd6')
        text = f'as {len(names)} ' + ' '.join(f'"{name}"' for name in names) + '\n'
        return Completed(0, text.encode(), b'')
    return run


def bus_searches(system) -> int:
    """How many times the store asked the stand-in bus which items carry an item's attributes (``SearchItems``)."""
    return sum(1 for argv, _ in system.calls if 'SearchItems' in argv or any('SearchItems' in w for w in argv))


def attribute_pairs(words) -> dict:
    return dict(zip(words[0::2], words[1::2]))


class FakeKeychain:
    """An in-memory Secret Service behind a fake ``secret-tool``, for FakeSystem.

    ``items`` maps an item's attribute words (a tuple) to its secret. A query matches an item as the Secret
    Service matches it: when the item's attributes include every pair asked for. ``lookup`` and ``clear`` act on
    unlocked items only, as libsecret does, and exit 1 silently when they find none; ``lookup`` first prompts to
    unlock a locked match, and ``unlock`` says how the prompt is answered: ``accept``, or ``cancel`` (libsecret
    then yields no path and no error, so secret-tool exits 1 silently). ``search --all`` lists every match, locked
    ones too, one ``[<n>]`` line each with the secret of an unlocked one on its own line, as secret-tool prints
    them; exit 0, and nothing printed when nothing matches.
    """

    def __init__(self) -> None:
        self.items: dict = {}
        self.locked: set = set()      # items in a locked collection
        self.unlock = 'accept'        # accept | cancel
        self.mode = 'ok'              # ok | locked | fail | dismissed | silent (every call exits 1, saying nothing)
        self.collection = (LOGIN, LOGIN_CREATED)   # which keychain it is, as the bus says (None: it does not say)
        self.after_store = None       # what ``store`` answers AFTER saving (a helper killed once it saved), or None
        self.searched = 'ok'          # ok | fail: what ``search`` does

    def _matches(self, words) -> list:
        query = attribute_pairs(words).items()
        return [key for key in self.items if query <= attribute_pairs(key).items()]

    def __call__(self, argv, input):
        action = argv[1]
        if self.mode == 'locked':
            return Completed(1, b'', b'secret-tool: Cannot create an item in a locked collection\n')
        if self.mode == 'dismissed':
            return Completed(1, b'', b'secret-tool: Prompt dismissed\n')
        if self.mode == 'fail':
            return Completed(1, b'', b'secret-tool: The name org.freedesktop.secrets was not provided\n')
        if self.mode == 'silent':
            return Completed(1, b'', b'')
        if action == 'store':
            key = tuple(argv[3:])
            self.items[key] = input
            self.locked.discard(key)
            return self.after_store or Completed(0, b'', b'')
        if action == 'search':
            if self.searched == 'fail':
                return Completed(1, b'', b'secret-tool: The name org.freedesktop.secrets was not provided\n')
            out = []
            for number, key in enumerate(self._matches([w for w in argv[2:] if w != '--all']), 1):
                if key in self.locked and '--all' not in argv:
                    continue
                out.append(f'[{number}]\nlabel = Recursion API key\n'.encode())
                if key not in self.locked:
                    out.append(b'secret = ' + self.items[key] + b'\n')
            return Completed(0, b''.join(out), b'')
        matches = self._matches(argv[2:])
        if action == 'lookup':
            for key in sorted(matches, key=lambda k: k in self.locked):
                if key in self.locked:
                    if self.unlock != 'accept':
                        return Completed(1, b'', b'')
                    self.locked.discard(key)
                return Completed(0, self.items[key], b'')
            return Completed(1, b'', b'')
        if action == 'clear':
            gone = [key for key in matches if key not in self.locked]
            for key in gone:
                del self.items[key]
            return Completed(0 if gone else 1, b'', b'')
        return Completed(2, b'', b'secret-tool: unknown action\n')


def keychain_system(**kw):
    """A FakeSystem with a session bus, a Secret Service on it, and an in-memory keychain."""
    chain = FakeKeychain()
    system = FakeSystem(tools={'busctl': busctl(owner=True, chain=chain), 'secret-tool': chain},
                        bus='unix:path=/fake/bus', **kw)
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
SEP = chr(31)
def pairs(words):
    return dict(zip(words[0::2], words[1::2]))
def matching(words):
    query = pairs(words).items()
    return [k for k in items if query <= pairs(k.split(SEP)).items()]
if action == 'store':
    items[SEP.join(rest)] = data.decode('ascii')
elif action == 'lookup':
    found = matching(rest)
    if not found:
        sys.exit(1)
    sys.stdout.write(items[found[0]]); sys.stdout.flush(); sys.exit(0)
elif action == 'clear':
    found = matching(rest)
    if not found:
        sys.exit(1)
    for k in found:
        del items[k]
elif action == 'search':
    for number, k in enumerate(matching([w for w in rest if w != '--all']), 1):
        sys.stdout.write('[%d]\nlabel = item\nsecret = %s\n' % (number, items[k]))
    sys.exit(0)
else:
    sys.stderr.write('secret-tool: unknown\n'); sys.exit(2)
with open(items_path, 'w') as fh:
    json.dump(items, fh)
'''

_BUSCTL = r'''#!{python} -I
import json, os, sys
STATE = {state!r}
if 'SearchItems' in sys.argv:
    words = sys.argv[sys.argv.index('a{{ss}}') + 2:]
    query = dict(zip(words[0::2], words[1::2])).items()
    path = os.path.join(STATE, 'items.json')
    items = json.load(open(path)) if os.path.exists(path) else {{}}
    found = [k for k in items if query <= dict(zip(k.split(chr(31))[0::2], k.split(chr(31))[1::2])).items()]
    print('aoao %d %s 0' % (len(found), ' '.join('"/org/freedesktop/secrets/collection/login/%d"' % (n + 1)
                                                  for n in range(len(found)))))
elif 'ReadAlias' in sys.argv:
    print('o "/org/freedesktop/secrets/collection/login"')
elif 'get-property' in sys.argv:
    print('t 1700000000')
elif 'ListNames' in sys.argv:
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
        programs['busctl'] = _BUSCTL.format(python=sys.executable, state=str(state))
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
