"""What the key store touches outside the process: the environment, helper programs, and which backend answers.

Everything here goes through one ``System`` object, so a test hands the store a fake one and nothing reaches
a real keychain, bus or terminal.

HELPERS. A helper program (``secret-tool``, ``busctl``, ``dbus-send``, ``journalctl``) is found only in the
ABSOLUTE folders of ``PATH`` (an empty or relative entry would run a program from whatever folder the store
was started in), started with ``shell=False`` from ``/``, and given a small environment of its own:
``PATH``, ``HOME``, the session-bus address, the runtime folder and the display names a keychain's unlock
prompt needs, with ``LC_ALL=C`` so its messages are the ones the store reads. Nothing else of the caller's
environment reaches it, so no engine's key in the daemon's environment does either. A key goes to a helper
only on its standard input, never in its arguments or its environment. A helper that cannot be started, runs
out of time or (for a stream) exits with a failure raises ``ToolMissing``, ``ToolTimeout`` or ``ToolFailed``,
each raised OUTSIDE the handler of the error underneath it, so no exception chain keeps a helper's output
(``subprocess.TimeoutExpired`` carries what the helper printed).

WHICH BACKEND ANSWERS (``detect``):

- macOS and Windows always have a keychain: ``macos_keychain`` and ``windows_credential``. Their backends
  are not built in this slice, so the store refuses there (``backend_not_built``) instead of writing a file.
- Elsewhere the keychain is the Secret Service, reached over the user's session bus. No session bus (no
  address in the environment and no ``$XDG_RUNTIME_DIR/bus`` socket): there is no keychain, ``owner_file``.
- With a bus, the bus itself is asked which names are owned or can be started (``ListNames`` and
  ``ListActivatableNames``, through ``busctl``, else ``dbus-send``):
  - ``org.freedesktop.secrets``: ``secret_service``, which needs ``secret-tool``; without it the store refuses
    (``keychain_tool_missing``) rather than write a file on a host that has a keychain;
  - no Secret Service but KWallet (``org.kde.kwalletd5`` or ``org.kde.kwalletd6``): refused
    (``keychain_unsupported``): a keychain is there, and this store reaches KWallet only through its Secret
    Service interface, which is off;
  - neither: ``owner_file``.
  Neither tool can answer: refused (``keychain_undetermined``), for the same reason.

A host where KWallet and the Secret Service both answer uses the Secret Service, the only Linux keychain this
store speaks to (Lucas's "Yes, fixed order" for the sibling product, Nov 12, 2026; this lane's brief asks the
same question again for Recursion).
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from recursion_core.keys.errors import KeyStoreError

SECRET_SERVICE_NAME = 'org.freedesktop.secrets'
KWALLET_NAMES = ('org.kde.kwalletd5', 'org.kde.kwalletd6')
HELPER_ENV_NAMES = ('PATH', 'HOME', 'DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR', 'DISPLAY', 'WAYLAND_DISPLAY')
BUS_TIMEOUT_S = 10.0
KEYCHAIN_TIMEOUT_S = 120.0     # an unlock prompt waits for the user


@dataclass(frozen=True)
class Completed:
    """A helper's exit status and output. ``stdout`` may hold a key: it is never logged or put in an error."""

    returncode: int
    stdout: bytes
    stderr: bytes

    def __repr__(self) -> str:
        return f'Completed(returncode={self.returncode}, stdout=<{len(self.stdout)} bytes>, stderr=<{len(self.stderr)} bytes>)'


class ToolMissing(Exception):
    """The helper program could not be started at all."""


class ToolTimeout(Exception):
    """The helper program ran out of time and was killed."""


class ToolFailed(Exception):
    """A streamed helper exited with a failure. Its argument is the exit status, nothing it printed."""


class System:
    """The real outside world. ``environ`` defaults to the process's environment, read at each call."""

    def __init__(self, environ=None, platform: str | None = None) -> None:
        self._environ = environ
        self.platform = platform or sys.platform

    @property
    def environ(self):
        return os.environ if self._environ is None else self._environ

    def home(self) -> Path:
        home = self.environ.get('HOME')
        return Path(home) if home and os.path.isabs(home) else Path.home()

    def session_bus(self) -> str | None:
        """The session bus address, or None when this process has no session bus."""
        address = self.environ.get('DBUS_SESSION_BUS_ADDRESS')
        if address:
            return address
        runtime = self.environ.get('XDG_RUNTIME_DIR')
        if runtime and os.path.isabs(runtime):
            socket = os.path.join(runtime, 'bus')
            try:
                if stat.S_ISSOCK(os.stat(socket).st_mode):
                    return f'unix:path={socket}'
            except OSError:
                return None
        return None

    def helper_env(self) -> dict:
        env = {name: self.environ[name] for name in HELPER_ENV_NAMES if self.environ.get(name)}
        env['PATH'] = os.pathsep.join(_absolute_path(self.environ.get('PATH', '')))
        bus = self.session_bus()
        if bus is not None:
            env['DBUS_SESSION_BUS_ADDRESS'] = bus
        env['LC_ALL'] = 'C'
        env['LANG'] = 'C'
        return env

    def which(self, name: str) -> str | None:
        folders = _absolute_path(self.environ.get('PATH', ''))
        return shutil.which(name, path=os.pathsep.join(folders)) if folders else None

    def run(self, argv: list, *, input: bytes | None = None, timeout: float) -> Completed:
        """Run a helper with its own small environment. Raises ``ToolMissing`` or ``ToolTimeout``."""
        name = os.path.basename(str(argv[0]))
        failure = None
        try:
            done = subprocess.run(argv, input=input, stdin=None if input is not None else subprocess.DEVNULL,
                                  capture_output=True, env=self.helper_env(), cwd='/', timeout=timeout,
                                  shell=False, close_fds=True, check=False)
        except subprocess.TimeoutExpired:
            failure = ToolTimeout          # the expired error holds the helper's output: it is not kept
        except OSError:
            failure = ToolMissing
        if failure is not None:
            raise failure(name)            # outside the handler: no __context__
        return Completed(done.returncode, done.stdout, done.stderr)

    def stream(self, argv: list):
        """Yield a helper's standard output in chunks (the journal can be large).

        Raises ``ToolMissing`` when it cannot be started and, once its output is exhausted, ``ToolFailed`` when it
        exited with a failure, so a reader can tell a search that ended from one that broke.
        """
        name = os.path.basename(str(argv[0]))
        try:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, env=self.helper_env(), cwd='/', shell=False,
                                       close_fds=True)
        except OSError:
            process = None
        if process is None:
            raise ToolMissing(name)
        finished = False
        try:
            while True:
                chunk = process.stdout.read(1 << 16)
                if not chunk:
                    break
                yield chunk
            finished = True
        finally:
            process.stdout.close()
            if not finished and process.poll() is None:
                process.kill()             # the reader stopped early
            status = process.wait()
        if status != 0:
            raise ToolFailed(status)

    def __repr__(self) -> str:
        return f'System(platform={self.platform!r})'


def _absolute_path(value: str) -> list:
    return [part for part in value.split(os.pathsep) if part and os.path.isabs(part)]


# ------------------------------------------------------------------------------------------- detection
def detect(system: System) -> str:
    """The backend this host offers now: a name from ``store.STORES``. Raises ``KeyStoreError``."""
    if system.platform == 'darwin':
        return 'macos_keychain'
    if system.platform == 'win32':
        return 'windows_credential'
    if system.session_bus() is None:
        return 'owner_file'
    names = bus_names(system)
    if names is None:
        raise KeyStoreError('keychain_undetermined',
                            'the session bus could not be asked whether a Secret Service is there '
                            '(neither busctl nor dbus-send answered); no key is written to a file on such a host')
    if SECRET_SERVICE_NAME not in names:
        if names & set(KWALLET_NAMES):
            raise KeyStoreError('keychain_unsupported',
                                'KWallet answers on the session bus without its Secret Service interface, which '
                                'this store needs; turn that interface on. No key is written to a file on such a host')
        return 'owner_file'
    if system.which('secret-tool') is None:
        raise KeyStoreError('keychain_tool_missing',
                            'a Secret Service is on the session bus but secret-tool is not installed '
                            '(it is in the libsecret-tools package); no key is written to a file on such a host')
    return 'secret_service'


def bus_names(system: System) -> set | None:
    """The names owned on the session bus or that it can start, or None when no tool could ask."""
    busctl = system.which('busctl')
    if busctl is not None:
        names = _ask(system, busctl, _busctl_names)
        if names is not None:
            return names
    dbus_send = system.which('dbus-send')
    if dbus_send is not None:
        return _ask(system, dbus_send, _dbus_send_names)
    return None


_SECRETS_PATH = '/org/freedesktop/secrets'
_COLLECTION = 'org.freedesktop.Secret.Collection'


def keychain_identity(system: System) -> tuple | None:
    """Which keychain the Secret Service saves into now: ``(path, created)``, the object path of its default collection
    (``ReadAlias("default")``, where ``secret-tool store`` puts an item) and that collection's ``Created`` property,
    in seconds. Read through ``busctl``, else ``dbus-send``; None when neither answers in that shape (a service that
    offers no default collection or no ``Created``). It reads properties only: no item, and nothing secret, so it
    answers while the collection is locked. A collection that was wiped and made again keeps its path and gets a new
    ``Created``; another keyring (another bus, a container) has its own. A ``Created`` of 0 is a service that does
    not say when the collection was made: two such keychains at one path could not be told apart, so it gives None."""
    for name, ask in (('busctl', _busctl_identity), ('dbus-send', _dbus_send_identity)):
        tool = system.which(name)
        if tool is not None:
            found = ask(system, tool)
            if found is not None:
                return found
    return None


def _object_path(text: str) -> str | None:
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    if text != '/' and text.startswith('/') and not text.endswith('/') and \
            all(part and all(c.isascii() and (c.isalnum() or c == '_') for c in part) for part in text[1:].split('/')):
        return text
    return None


def _busctl_identity(system: System, tool: str) -> tuple | None:
    """``busctl --user call ... ReadAlias s default`` prints ``o "<path>"``; ``get-property ... Created`` ``t <n>``."""
    text = _call(system, [tool, '--user', 'call', SECRET_SERVICE_NAME, _SECRETS_PATH, 'org.freedesktop.Secret.Service',
                          'ReadAlias', 's', 'default'])
    words = text.split() if text else []
    path = _object_path(words[1]) if len(words) == 2 and words[0] == 'o' else None
    if path is None:
        return None
    text = _call(system, [tool, '--user', 'get-property', SECRET_SERVICE_NAME, path, _COLLECTION, 'Created'])
    words = text.split() if text else []
    if len(words) != 2 or words[0] != 't' or not (words[1].isascii() and words[1].isdigit()):
        return None
    return (path, int(words[1])) if int(words[1]) > 0 else None     # 0: the service does not say


def _dbus_send_identity(system: System, tool: str) -> tuple | None:
    """``dbus-send --print-reply`` prints ``object path "<path>"``, then ``variant uint64 <n>``."""
    text = _call(system, [tool, '--session', '--print-reply', f'--dest={SECRET_SERVICE_NAME}', _SECRETS_PATH,
                          'org.freedesktop.Secret.Service.ReadAlias', 'string:default'])
    words = text.split() if text else []
    path = _object_path(words[-1]) if len(words) >= 3 and words[-3:-1] == ['object', 'path'] else None
    if path is None:
        return None
    text = _call(system, [tool, '--session', '--print-reply', f'--dest={SECRET_SERVICE_NAME}', path,
                          'org.freedesktop.DBus.Properties.Get', f'string:{_COLLECTION}', 'string:Created'])
    words = text.split() if text else []
    if len(words) < 3 or words[-3:-1] != ['variant', 'uint64'] or not (words[-1].isascii() and words[-1].isdigit()):
        return None
    return (path, int(words[-1])) if int(words[-1]) > 0 else None   # 0: the service does not say


def keychain_items(system: System, attributes: list) -> int | None:
    """How many items of the Secret Service carry every pair of ``attributes`` (a flat list: a name, then its value),
    in any collection and locked ones too: the service's ``SearchItems``, asked through ``busctl``, else ``dbus-send``.
    It answers with the items' object paths only (unlocked, then locked), never with a secret, and it opens no session
    and raises no unlock prompt: so it learns that an item is there without reading its value. None when neither tool
    answers in that shape."""
    pairs = list(zip(attributes[0::2], attributes[1::2]))
    for name, ask in (('busctl', _busctl_items), ('dbus-send', _dbus_send_items)):
        tool = system.which(name)
        if tool is not None:
            found = ask(system, tool, pairs)
            if found is not None:
                return found
    return None


def _busctl_items(system: System, tool: str, pairs: list) -> int | None:
    """``busctl --user call ... SearchItems a{ss} <n> <name> <value> ...`` prints the reply's signature, then each
    array as its length and its quoted paths: ``aoao 1 "<path>" 0``."""
    text = _call(system, [tool, '--user', 'call', SECRET_SERVICE_NAME, _SECRETS_PATH, 'org.freedesktop.Secret.Service',
                          'SearchItems', 'a{ss}', str(len(pairs)), *[word for pair in pairs for word in pair]])
    words = text.split() if text else []
    if words[:1] == ['aoao']:
        words = words[1:]
    total = 0
    for _ in range(2):                           # the unlocked items, then the locked ones
        if words[:1] == ['ao']:
            words = words[1:]
        if not words or not (words[0].isascii() and words[0].isdigit()):
            return None
        length = int(words[0])
        paths, words = words[1:1 + length], words[1 + length:]
        if len(paths) != length or not all(path[:1] == '"' and _object_path(path) for path in paths):
            return None
        total += length
    return None if words else total


def _dbus_send_items(system: System, tool: str, pairs: list) -> int | None:
    """``dbus-send --print-reply`` prints a ``method return`` line, then two ``array [`` ... ``]`` blocks of
    ``object path "<path>"`` lines (the unlocked items, then the locked ones)."""
    if any(',' in word for pair in pairs for word in pair):
        return None                              # its dict argument is split at commas: such a word cannot be asked
    text = _call(system, [tool, '--session', '--print-reply', f'--dest={SECRET_SERVICE_NAME}', _SECRETS_PATH,
                          'org.freedesktop.Secret.Service.SearchItems',
                          'dict:string:string:' + ','.join(word for pair in pairs for word in pair)])
    lines = [line.split() for line in (text or '').splitlines()[1:] if line.strip()]
    if not text or [words for words in lines if words in (['array', '['], [']'])] != [['array', '['], [']']] * 2:
        return None
    paths = [words for words in lines if words not in (['array', '['], [']'])]
    if not all(len(words) == 3 and words[:2] == ['object', 'path'] and _object_path(words[2]) for words in paths):
        return None
    return len(paths)


def _call(system: System, argv: list) -> str | None:
    try:
        done = system.run(argv, timeout=BUS_TIMEOUT_S)
    except (ToolMissing, ToolTimeout):
        return None
    if done.returncode != 0:
        return None
    try:
        return done.stdout.decode('ascii')
    except UnicodeDecodeError:
        return None


_BUS = ('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus')


def _ask(system: System, tool: str, parse) -> set | None:
    names = set()
    for method in ('ListNames', 'ListActivatableNames'):
        part = parse(system, tool, method)
        if part is None:
            return None
        names |= part
    return names


def _busctl_names(system: System, tool: str, method: str) -> set | None:
    """``busctl --user call ... ListNames`` prints ``as <n> "name" "name" ...``."""
    text = _call(system, [tool, '--user', 'call', *_BUS, method])
    if text is None:
        return None
    words = text.split()
    if len(words) < 2 or words[0] != 'as' or not words[1].isdigit():
        return None
    names = {word[1:-1] for word in words[2:] if len(word) >= 2 and word[0] == word[-1] == '"'}
    return names if len(names) == int(words[1]) else None


def _dbus_send_names(system: System, tool: str, method: str) -> set | None:
    """``dbus-send --print-reply`` prints a ``method return`` line, then ``array [``, ``string "name"`` lines, ``]``."""
    text = _call(system, [tool, '--session', '--print-reply', '--dest=org.freedesktop.DBus', '/org/freedesktop/DBus',
                          f'org.freedesktop.DBus.{method}'])
    if text is None or 'array [' not in text:
        return None
    names = set()
    for line in text.splitlines():
        words = line.split()
        if len(words) == 2 and words[0] == 'string' and len(words[1]) >= 2 and words[1][0] == words[1][-1] == '"':
            names.add(words[1][1:-1])
    return names
