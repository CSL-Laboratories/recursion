"""What the key store touches outside the process: the environment, helper programs, and which backend answers.

Everything here goes through one ``System`` object, so a test hands the store a fake one and nothing reaches
a real keychain, bus or terminal.

HELPERS. A helper program (``secret-tool``, ``busctl``, ``dbus-send``, ``journalctl``) is found only in the
ABSOLUTE folders of ``PATH`` (an empty or relative entry would run a program from whatever folder the store
was started in), started with ``shell=False`` from ``/``, and given a small environment of its own:
``PATH``, ``HOME``, the session-bus address, the runtime folder and the display names a keychain's unlock
prompt needs, with ``LC_ALL=C`` so its messages are the ones the store reads. Nothing else of the caller's
environment reaches it, so no engine's key in the daemon's environment does either. A key goes to a helper
only on its standard input, never in its arguments or its environment.

WHICH BACKEND ANSWERS (``detect``):

- macOS and Windows always have a keychain: ``macos_keychain`` and ``windows_credential``. macOS needs the
  ``security`` tool (refused ``keychain_tool_missing`` without it). The Windows backend is not built yet, so the
  store refuses there (``backend_not_built``) instead of writing a file.
- Elsewhere the keychain is the Secret Service, reached over the user's session bus. No session bus (no
  address in the environment and no ``$XDG_RUNTIME_DIR/bus`` socket): there is no keychain, ``owner_file``.
- With a bus, the bus itself is asked whether ``org.freedesktop.secrets`` has an owner or can be started
  (``busctl``, else ``dbus-send``). No: ``owner_file``. Yes: ``secret_service``, which needs ``secret-tool``;
  without it the store refuses (``keychain_tool_missing``) rather than write a file on a host that has a
  keychain. Neither tool can answer: refused (``keychain_undetermined``), for the same reason.

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
        try:
            done = subprocess.run(argv, input=input, stdin=None if input is not None else subprocess.DEVNULL,
                                  capture_output=True, env=self.helper_env(), cwd='/', timeout=timeout,
                                  shell=False, close_fds=True, check=False)
        except subprocess.TimeoutExpired:
            raise ToolTimeout(os.path.basename(str(argv[0]))) from None
        except OSError:
            raise ToolMissing(os.path.basename(str(argv[0]))) from None
        return Completed(done.returncode, done.stdout, done.stderr)

    def stream(self, argv: list):
        """Yield a helper's standard output in chunks (the journal can be large). Raises ``ToolMissing``."""
        try:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, env=self.helper_env(), cwd='/', shell=False,
                                       close_fds=True)
        except OSError:
            raise ToolMissing(os.path.basename(str(argv[0]))) from None
        try:
            while True:
                chunk = process.stdout.read(1 << 16)
                if not chunk:
                    break
                yield chunk
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
            process.wait()

    def __repr__(self) -> str:
        return f'System(platform={self.platform!r})'


def _absolute_path(value: str) -> list:
    return [part for part in value.split(os.pathsep) if part and os.path.isabs(part)]


# ------------------------------------------------------------------------------------------- detection
def detect(system: System) -> str:
    """The backend this host offers now: a name from ``store.STORES``. Raises ``KeyStoreError``."""
    if system.platform == 'darwin':
        if system.which('security') is None:
            raise KeyStoreError('keychain_tool_missing', 'the macOS security tool was not found; no key is written to '
                                'a file on a host that has a keychain')
        return 'macos_keychain'
    if system.platform == 'win32':
        return 'windows_credential'
    if system.session_bus() is None:
        return 'owner_file'
    answer = secret_service_on_bus(system)
    if answer is None:
        raise KeyStoreError('keychain_undetermined',
                            'the session bus could not be asked whether a Secret Service is there '
                            '(neither busctl nor dbus-send answered); no key is written to a file on such a host')
    if not answer:
        return 'owner_file'
    if system.which('secret-tool') is None:
        raise KeyStoreError('keychain_tool_missing',
                            'a Secret Service is on the session bus but secret-tool is not installed '
                            '(it is in the libsecret-tools package); no key is written to a file on such a host')
    return 'secret_service'


def secret_service_on_bus(system: System) -> bool | None:
    """True when ``org.freedesktop.secrets`` has an owner or can be started, False when neither, None when
    no tool could ask."""
    busctl = system.which('busctl')
    if busctl is not None:
        answer = _ask_busctl(system, busctl)
        if answer is not None:
            return answer
    dbus_send = system.which('dbus-send')
    if dbus_send is not None:
        return _ask_dbus_send(system, dbus_send)
    return None


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


def _ask_busctl(system: System, tool: str) -> bool | None:
    owner = _call(system, [tool, '--user', 'call', *_BUS, 'NameHasOwner', 's', SECRET_SERVICE_NAME])
    if owner is None or owner.strip() not in ('b true', 'b false'):
        return None
    if owner.strip() == 'b true':
        return True
    names = _call(system, [tool, '--user', 'call', *_BUS, 'ListActivatableNames'])
    if names is None or not names.startswith('as '):
        return None
    return f'"{SECRET_SERVICE_NAME}"' in names.split()


def _ask_dbus_send(system: System, tool: str) -> bool | None:
    base = [tool, '--session', '--print-reply', '--dest=org.freedesktop.DBus', '/org/freedesktop/DBus']
    owner = _call(system, [*base, 'org.freedesktop.DBus.NameHasOwner', f'string:{SECRET_SERVICE_NAME}'])
    if owner is None:
        return None
    words = owner.split()
    if words[-2:] == ['boolean', 'true']:
        return True
    if words[-2:] != ['boolean', 'false']:
        return None
    names = _call(system, [*base, 'org.freedesktop.DBus.ListActivatableNames'])
    if names is None or 'array' not in names.split():
        return None
    return f'"{SECRET_SERVICE_NAME}"' in names.split()
