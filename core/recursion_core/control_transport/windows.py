"""Windows control transport: STUB, not implemented (F40 follow-on).

Windows 10+ has AF_UNIX, but no SO_PEERCRED, and its socket-file ACLs do not follow POSIX
0700/0600 modes, so the Linux checks here would pass without protecting anything. The
Windows path (likely a named pipe with an owner-only DACL and
``GetNamedPipeClientProcessId`` plus token-SID comparison) is the F40 follow-on. Until it
lands, both classes raise ``PlatformNotSupported`` (a ``NotImplementedError``) on
construction, so nothing silently falls back to TCP.
"""

from __future__ import annotations

from typing import Any

from .errors import PlatformNotSupported

WINDOWS_STUB_MESSAGE = (
    "control transport on Windows is not implemented: stub pending the F40 follow-on "
    "(owner-only named pipe with client SID check); Linux is the tested path"
)


class WindowsControlServer:
    """Stub. Raises ``PlatformNotSupported`` (F40 follow-on)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise PlatformNotSupported(WINDOWS_STUB_MESSAGE)


class WindowsControlClient:
    """Stub. Raises ``PlatformNotSupported`` (F40 follow-on)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise PlatformNotSupported(WINDOWS_STUB_MESSAGE)
