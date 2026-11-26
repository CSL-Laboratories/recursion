"""Control API client over the daemon's owner-only Unix socket (CSL-798 / CSL-738 composition).

``SocketAPI.call(method, params)`` has the same shape as ``ControlAPI.call``, so the CLI (and any
other surface on this machine) prints what the daemon answered and adds nothing. One connection per
call: the transport closes a client after any failure, and a CLI makes one call.

When the daemon cannot be reached, or does not answer, the result is ``control_api_unavailable`` with
what is known about delivery. A Stop that got no answer is never reported as stopped (PRODUCT.md
d84cdd26, CSLR-D-012, section 3.4 S6 line 888):

* ``delivered: false``: nothing listened, the peer was refused, the server was at its connection
  cap, or the request could not be framed at all (over the frame cap, or not JSON: detail
  ``not_sent:<reason>``, checked before anything is connected). The request did not reach the
  control API.
* ``delivered: "unknown"``: the request was written and no complete reply came back. It may have
  been carried out; read the run's status before retrying.

On a platform without the Unix transport (Windows) every call answers ``control_api_unavailable``
with ``delivered: false`` and ``detail: "platform_unsupported"``; nothing is connected.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from recursion_core.control_transport import (FrameError, PeerNotAdmitted, PlatformNotSupported, TransportBusy,
                                              TransportRefused, UnixControlClient)
from recursion_core.control_transport.framing import encode_frame

SOCKET_DIR = "recursion"  # $XDG_RUNTIME_DIR/recursion/control.sock
STATE_DIR = "recursion"  # $XDG_STATE_HOME/recursion
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


def state_directory(environ=None) -> str:
    """The daemon's state folder: ``$XDG_STATE_HOME/recursion``, else ``~/.local/state/recursion``.

    The one place this rule is written (the daemon and the relay both read it). With ``environ`` given,
    ``HOME`` is read from it too, and a mapping with neither ``XDG_STATE_HOME`` nor ``HOME`` is refused
    (``ValueError``): nothing then falls back to the home folder of the process.
    """
    if environ is None:
        base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    else:
        base = environ.get("XDG_STATE_HOME") or (str(Path(environ["HOME"]) / ".local/state")
                                                 if environ.get("HOME") else None)
        if base is None:
            raise ValueError("no state folder: neither XDG_STATE_HOME nor HOME is set")
    return str(Path(base) / STATE_DIR)


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
        if params is not None and not isinstance(params, Mapping):
            # The params are an object on the wire. Anything else is refused here, typed and before anything
            # is connected (the reads of #377 and #385 by DeepSeek V4.1 Flash, F9 and F6).
            return {"code": UNAVAILABLE, "delivered": False, "detail": "not_sent:params_not_object"}
        message = {"op": "control", "method": method, "params": dict(params or {})}
        try:
            # A request the transport cannot frame is refused before any byte is written, so it was
            # not delivered; reported after the write it read as "unknown" (Nov 12 review, finding 8).
            encode_frame(message)
        except FrameError as err:
            return {"code": UNAVAILABLE, "delivered": False, "detail": f"not_sent:{err}"}
        except (TypeError, ValueError):
            return {"code": UNAVAILABLE, "delivered": False, "detail": "not_sent:frame_not_json"}
        try:
            client = self._connect(self.path, timeout_s=self._timeout_s)
        except PlatformNotSupported:
            return {"code": UNAVAILABLE, "delivered": False, "detail": "platform_unsupported"}
        except (OSError, TransportRefused) as err:
            return {"code": UNAVAILABLE, "delivered": False, "detail": f"connect:{type(err).__name__}"}
        try:
            reply = client.request(message)
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
