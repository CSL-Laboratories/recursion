"""Typed refusals for the control transport (CSL-719; D02, D50, CX-026)."""

from __future__ import annotations


class TransportRefused(Exception):
    """Base class: the transport refused to start, accept or carry something."""


class RemoteBindRefused(TransportRefused, ValueError):
    """A network or non-AF_UNIX address was asked for.

    CX-026: there is no pre-auth remote face. Remote control reaches this socket over an
    SSH stdio relay, never over a listener this package opens.
    """


class InsecureSocketPath(TransportRefused, ValueError):
    """The socket path or its directory fails the owner-only checks (0700 dir, 0600 socket)."""


class PeerRefused(TransportRefused, PermissionError):
    """The other end's uid is not ours, or its credentials could not be read."""


class FrameError(TransportRefused, ValueError):
    """A frame is malformed, oversized or truncated.

    ``id_known`` is true when the body yielded a top-level id, including null.
    An unparseable frame leaves it false. The reply then omits ``id``: there is
    nothing to echo, and the server does not invent one.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.id_known = False
        self.request_id: object = None


class PlatformNotSupported(TransportRefused, NotImplementedError):
    """The transport has no tested implementation on this platform (Windows: F40)."""


class TransportBusy(TransportRefused, ConnectionError):
    """The server was at its connection cap and sent a typed ``busy`` frame.

    The request was not delivered: no handler saw it. A surface can report a Stop sent
    into this as not delivered (S6 "pending"), never as stopped.
    """
