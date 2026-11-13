"""Local control transport for the new core (CSL-719; D02, D50, CX-026).

Linux: tested. macOS: designed in (same AF_UNIX family, ``LOCAL_PEERCRED``), not tested
here. Windows: labelled stub raising ``PlatformNotSupported`` (F40 follow-on).
There is no network listener; network-shaped addresses raise ``RemoteBindRefused``.
"""

from __future__ import annotations

import sys
from typing import Any

from .errors import (
    FrameError,
    InsecureSocketPath,
    PeerNotAdmitted,
    PeerRefused,
    PlatformNotSupported,
    RemoteBindRefused,
    TransportBusy,
    TransportRefused,
)
from .handlers import Connection, with_ping_health
from .peercred import PeerCred, get_peer_cred
from .unix import TransportClient, TransportServer, UnixControlClient, UnixControlServer, refuse_remote_bind
from .windows import WindowsControlClient, WindowsControlServer


def _is_windows(platform: str) -> bool:
    return platform.startswith("win") or platform == "cygwin"


def create_server(directory: object, handler: Any, *, platform: str | None = None, **kwargs: Any) -> TransportServer:
    """The platform's server; Windows raises ``PlatformNotSupported`` (F40)."""
    if _is_windows(platform or sys.platform):
        return WindowsControlServer(directory, handler, **kwargs)
    return UnixControlServer(directory, handler, **kwargs)


def create_client(path: object, *, platform: str | None = None, **kwargs: Any) -> TransportClient:
    """The platform's client; Windows raises ``PlatformNotSupported`` (F40)."""
    if _is_windows(platform or sys.platform):
        return WindowsControlClient(path, **kwargs)
    return UnixControlClient(path, **kwargs)


__all__ = [
    "Connection",
    "FrameError",
    "InsecureSocketPath",
    "PeerCred",
    "PeerNotAdmitted",
    "PeerRefused",
    "PlatformNotSupported",
    "RemoteBindRefused",
    "TransportBusy",
    "TransportClient",
    "TransportRefused",
    "TransportServer",
    "UnixControlClient",
    "UnixControlServer",
    "WindowsControlClient",
    "WindowsControlServer",
    "create_client",
    "create_server",
    "get_peer_cred",
    "refuse_remote_bind",
    "with_ping_health",
]
