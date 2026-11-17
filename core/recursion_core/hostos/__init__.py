"""Start-path hooks for each host the daemon can be asked to run on.

``select`` returns the hooks the daemon entry and the control client use at
startup. Linux keeps the calls those two already make. macOS checks that this
process is a LaunchAgent and uses a short private runtime directory. Windows
raises the same typed refusal the daemon already raises there.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys

# Darwin's AF_UNIX sun_path is 104 bytes. The runtime directory stays within
# 60 so a socket name placed under it still fits, including the trailing NUL.
MAX_RUNTIME_DIR_BYTES = 60
_LAUNCH_AGENT_REFUSAL = 'run as the Recursion LaunchAgent'
_RUNTIME_REFUSAL = 'runtime directory must be a private 0700 path of at most 60 bytes'
_DAEMON_REFUSAL = (
    'platform_unsupported: the user daemon needs Linux '
    '(flock, uid ownership checks, O_NOFOLLOW, systemd user units)'
)
_LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')


def select(platform=None):
    """Return the start-path hooks for ``platform`` (this interpreter, when omitted)."""
    if platform is None:
        platform = sys.platform
    if platform == 'darwin':
        return DarwinHooks()
    if platform == 'win32':
        return Win32Hooks()
    # Linux, and any other platform, keep today's calls. Darwin is the only
    # POSIX host whose start path differs.
    return LinuxHooks()


class LinuxHooks:
    """Today's Linux start path."""

    def check_operator(self, user, call=subprocess.run, environ=None):
        """Read-only linger probe. Never enables or disables lingering."""
        refusal = f'Linger=yes required for operator {user}; founder host setup needed'
        try:
            result = call(['loginctl', 'show-user', user, '--property=Linger', '--value'],
                          check=True, text=True, capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            # logind unknown user, logind not running, loginctl missing or hung: fail closed
            # with the named refusal, not a raw traceback.
            raise RuntimeError(f'{refusal} (linger probe failed: {type(exc).__name__}: {exc})') from exc
        if result.stdout.strip() != 'yes':
            raise RuntimeError(refusal)

    def runtime_dir(self, environ=None):
        """``$XDG_RUNTIME_DIR`` when it is set, otherwise ``/run/user/<uid>``."""
        environ = os.environ if environ is None else environ
        return environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'


class DarwinHooks:
    """A LaunchAgent check, and a private runtime directory of at most 60 bytes."""

    def check_operator(self, user, call=subprocess.run, environ=None):
        """The process must be the Recursion LaunchAgent.

        ``user`` is accepted so every platform shares one call. The read itself
        is the label in ``XPC_SERVICE_NAME`` and ``launchctl print gui/<uid>/<label>``.
        """
        environ = os.environ if environ is None else environ
        label = environ.get('XPC_SERVICE_NAME', '').strip()
        if not _LABEL.fullmatch(label) or not hasattr(os, 'getuid'):
            raise RuntimeError(_LAUNCH_AGENT_REFUSAL)
        service = f'gui/{os.getuid()}/{label}'
        try:
            result = call(['launchctl', 'print', service],
                          check=True, text=True, capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(_LAUNCH_AGENT_REFUSAL) from exc
        stdout = result.stdout if isinstance(getattr(result, 'stdout', ''), str) else ''
        if service not in stdout or 'state = running' not in stdout:
            raise RuntimeError(_LAUNCH_AGENT_REFUSAL)

    def runtime_dir(self, environ=None):
        """A private 0700 directory of at most 60 bytes, never ``/run/user/<uid>``.

        ``$XDG_RUNTIME_DIR`` is used when it already meets that rule. Otherwise
        the directory is ``/tmp/recursion-<uid>``.
        """
        environ = os.environ if environ is None else environ
        chosen = environ.get('XDG_RUNTIME_DIR')
        if not chosen:
            if not hasattr(os, 'getuid'):
                raise RuntimeError(_RUNTIME_REFUSAL)
            chosen = f'/tmp/recursion-{os.getuid()}'
        return _ensure_private_runtime(chosen)


class Win32Hooks:
    """The daemon's existing Windows refusal, with the same text."""

    def check_operator(self, user, call=subprocess.run, environ=None):
        _raise_daemon_unsupported()

    def runtime_dir(self, environ=None):
        _raise_daemon_unsupported()


def _raise_daemon_unsupported():
    from recursion_core.daemon import PlatformUnsupported

    raise PlatformUnsupported(_DAEMON_REFUSAL)


def _ensure_private_runtime(path):
    if not isinstance(path, str) or '\0' in path or not path.startswith('/') or path == '/':
        raise RuntimeError(_RUNTIME_REFUSAL)
    path = path.rstrip('/')
    parts = path.split('/')
    if any(part in ('', '.', '..') for part in parts[1:]):
        raise RuntimeError(_RUNTIME_REFUSAL)
    try:
        raw = os.fsencode(path)
    except UnicodeEncodeError as exc:
        raise RuntimeError(_RUNTIME_REFUSAL) from exc
    if not raw or len(raw) > MAX_RUNTIME_DIR_BYTES:
        raise RuntimeError(_RUNTIME_REFUSAL)
    if not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_DIRECTORY') or not hasattr(os, 'geteuid'):
        raise RuntimeError(_RUNTIME_REFUSAL)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise RuntimeError(_RUNTIME_REFUSAL) from exc
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise RuntimeError(_RUNTIME_REFUSAL) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise RuntimeError(_RUNTIME_REFUSAL)
        if stat.S_IMODE(info.st_mode) != 0o700:
            try:
                os.fchmod(fd, 0o700)
            except OSError as exc:
                raise RuntimeError(_RUNTIME_REFUSAL) from exc
            info = os.fstat(fd)
        if stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.geteuid():
            raise RuntimeError(_RUNTIME_REFUSAL)
    finally:
        os.close(fd)
    return path
