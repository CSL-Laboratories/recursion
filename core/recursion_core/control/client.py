"""Control API client over the daemon's owner-only Unix socket (CSL-798 / CSL-738 composition).

``SocketAPI.call(method, params)`` has the same shape as ``ControlAPI.call``, so the CLI (and any
other surface on this machine) prints what the daemon answered and adds nothing. One connection per
call: the transport closes a client after any failure, and a CLI makes one call.

When the daemon cannot be reached, or does not answer, the result is ``control_api_unavailable`` with
what is known about delivery. A Stop that got no answer is never reported as stopped (PRODUCT.md
d84cdd26, CSLR-D-012, section 3.4 S6 line 888):

* ``delivered: false``: nothing listened, the peer was refused, or the server was at its connection
  cap. The request did not reach the control API.
* ``delivered: "unknown"``: the request was written and no complete reply came back. It may have
  been carried out; read the run's status before retrying.

On a platform without the Unix transport (Windows) every call answers ``control_api_unavailable``
with ``delivered: false`` and ``detail: "platform_unsupported"``; nothing is connected.
"""

from __future__ import annotations

import os
from pathlib import Path

from recursion_core.control_transport import (PeerNotAdmitted, PlatformNotSupported, TransportBusy, TransportRefused,
                                              UnixControlClient)

SOCKET_DIR = "recursion"  # $XDG_RUNTIME_DIR/recursion/control.sock
UNAVAILABLE = "control_api_unavailable"
# A Stop waits for the #916 procedure (about 2 s of SIGINT grace, then SIGKILL) and its confirmation;
# a start waits for the unit to launch. The reply deadline has to cover both.
DEFAULT_TIMEOUT_S = 60.0


def socket_directory(environ=None) -> str:
    environ = os.environ if environ is None else environ
    if os.name == "nt" or not hasattr(os, "getuid"):
        raise PlatformNotSupported(
            "platform_unsupported: the daemon's control socket needs POSIX (AF_UNIX under the user's runtime directory)"
        )
    runtime = environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return str(Path(runtime) / SOCKET_DIR)


def socket_path(environ=None) -> str:
    return str(Path(socket_directory(environ)) / "control.sock")


class SocketAPI:
    def __init__(self, path: str | None = None, *, timeout_s: float = DEFAULT_TIMEOUT_S, connect=UnixControlClient) -> None:
        try:
            self.path = path if path is not None else socket_path()
        except PlatformNotSupported:  # Windows: there is no socket to name; every call answers the typed refusal
            self.path = None
        self._timeout_s, self._connect = timeout_s, connect

    def call(self, method: str, params: dict | None = None) -> dict:
        if self.path is None:
            return {"code": UNAVAILABLE, "delivered": False, "detail": "platform_unsupported"}
        try:
            client = self._connect(self.path, timeout_s=self._timeout_s)
        except PlatformNotSupported:
            return {"code": UNAVAILABLE, "delivered": False, "detail": "platform_unsupported"}
        except (OSError, TransportRefused) as err:
            return {"code": UNAVAILABLE, "delivered": False, "detail": f"connect:{type(err).__name__}"}
        try:
            reply = client.request({"op": "control", "method": method, "params": dict(params or {})})
        except TransportBusy:
            return {"code": UNAVAILABLE, "delivered": False, "detail": "busy"}
        except PeerNotAdmitted:  # refused at accept (a peer inside a run unit): no handler saw the request
            return {"code": UNAVAILABLE, "delivered": False, "detail": "daemon:scope_refused"}
        except (OSError, TransportRefused) as err:
            return {"code": UNAVAILABLE, "delivered": "unknown", "detail": f"request:{type(err).__name__}"}
        finally:
            client.close()
        result = reply.get("result") if reply.get("ok") is True else None
        if isinstance(result, dict) and isinstance(result.get("code"), str):
            return result
        error = reply.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        # The daemon saw the request and refused it before the control API (scope, bad op).
        return {"code": UNAVAILABLE, "delivered": True, "detail": f"daemon:{code if isinstance(code, str) else 'invalid_reply'}"}
