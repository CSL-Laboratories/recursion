"""The check the server must pass before it serves: the daemon's owner-only control socket, as it is today.

The server is a client of the socket ``control/client.py`` names (``$XDG_RUNTIME_DIR/recursion/control.sock``,
or ``/run/user/<uid>/recursion/control.sock`` when the variable is unset). It does not start unless all
of these hold, checked in this order, each with its own refusal reason:

1. the platform has the Unix transport (``platform_unsupported``; Windows today);
2. the socket path is absolute (``socket_path_not_absolute``);
3. its directory exists (``socket_dir_missing``), is a real directory and not a symlink
   (``socket_dir_not_a_directory``), belongs to this user (``socket_dir_foreign_owner``) and grants no
   group or other bit (``socket_dir_mode``), as ``control_transport.unix.ensure_private_dir`` requires of
   the server's own directory; nothing is created or repaired;
4. the socket exists (``socket_missing``), is a socket and not a symlink (``socket_not_a_socket``),
   belongs to this user (``socket_foreign_owner``) and grants no group or other bit (``socket_mode``);
   the daemon's server makes it 0600;
5. one read through the socket gets an answer in the control API's shape, an object with a string
   ``code`` (``daemon_unreachable``, with the client's detail): ``run.list`` with ``limit: 1``. The
   connection itself checks that the listening process runs as this user (``UnixControlClient``,
   SO_PEERCRED), and the daemon refuses a peer inside a run unit (``daemon:scope_refused``), so a server
   started by an engine inside a run does not start. An answer that is not a success
   (``registry_unavailable``) still passes. This proves the socket is sound and that a process of this
   user answers on it in that shape; it does not prove which program that is (only this user can put a
   listener inside the 0700 folder).

Steps 3 and 4 are repeated before every call the server forwards (``CheckedSocketAPI``): a directory or
socket that stops passing answers ``control_api_unavailable`` with ``delivered: false`` and nothing is
sent. ``CheckedSocketAPI`` also encodes, before connecting, the frame ``SocketAPI`` would write
(``{"op": "control", "method": ..., "params": ...}``, ``control_transport.framing.encode_frame``). A call
the transport could never carry answers ``control_api_unavailable`` with ``delivered: false``, since nothing
was written (``SocketAPI`` itself would answer ``delivered: "unknown"`` for some of them): over its 1 MiB
frame or nested too deep (the frame error as ``detail``, ``request:frame_too_large``); a string UTF-8
cannot encode, such as a lone surrogate, which JSON can carry (``request:frame_unencodable:...``); ``params``
that are not an object (``request:params_not_an_object``).

There is no token and no scope here: the daemon grants a same-user peer every verb, as it does the CLI.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Mapping
from typing import Any

from recursion_core.control.client import UNAVAILABLE, SocketAPI, socket_path
from recursion_core.control_transport import PlatformNotSupported
from recursion_core.control_transport.errors import FrameError
from recursion_core.control_transport.framing import encode_frame

PROBE = ("run.list", {"limit": 1})


class SocketCheckFailed(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason, self.detail = reason, detail


def locate(environ: Mapping[str, str] | None = None) -> str:
    """The control socket's path, or a refusal (``platform_unsupported``, ``socket_path_not_absolute``)."""

    try:
        path = socket_path(environ)
    except PlatformNotSupported as exc:
        raise SocketCheckFailed("platform_unsupported", str(exc)) from None
    if not hasattr(os, "geteuid"):
        raise SocketCheckFailed("platform_unsupported", "no os.geteuid on this platform")
    if not os.path.isabs(path):
        raise SocketCheckFailed("socket_path_not_absolute", path)
    return path


def _private(st: os.stat_result, prefix: str, where: str) -> None:
    if st.st_uid != os.geteuid():
        raise SocketCheckFailed(f"{prefix}_foreign_owner", f"{where} belongs to uid {st.st_uid}")
    if st.st_mode & 0o077:
        raise SocketCheckFailed(f"{prefix}_mode", f"{where} has mode {stat.S_IMODE(st.st_mode):04o}")


def check_path(path: str) -> None:
    """Steps 3 and 4: the directory and the socket file, by ``lstat`` (no symlink is followed)."""

    directory = os.path.dirname(path)
    try:
        st = os.lstat(directory)
    except FileNotFoundError:
        raise SocketCheckFailed("socket_dir_missing", directory) from None
    except OSError as exc:
        raise SocketCheckFailed("socket_dir_missing", f"{directory}: {type(exc).__name__}") from None
    if not stat.S_ISDIR(st.st_mode):  # lstat: a symlink is not a directory
        raise SocketCheckFailed("socket_dir_not_a_directory", directory)
    _private(st, "socket_dir", directory)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise SocketCheckFailed("socket_missing", path) from None
    except OSError as exc:
        raise SocketCheckFailed("socket_missing", f"{path}: {type(exc).__name__}") from None
    if not stat.S_ISSOCK(st.st_mode):
        raise SocketCheckFailed("socket_not_a_socket", path)
    _private(st, "socket", path)


class CheckedSocketAPI:
    """``SocketAPI`` with steps 3 and 4 repeated before every call, and a frame the transport would refuse
    answered ``delivered: false`` before anything is written."""

    def __init__(self, path: str, *, socket_api: Any = None) -> None:
        self.path = path
        self._api = socket_api if socket_api is not None else SocketAPI(path)

    def call(self, method: str, params: dict | None = None) -> dict:
        try:
            check_path(self.path)
        except SocketCheckFailed as exc:
            return {"code": UNAVAILABLE, "delivered": False, "detail": f"socket_check:{exc.reason}"}
        if params is not None and not isinstance(params, Mapping):
            return {"code": UNAVAILABLE, "delivered": False, "detail": "request:params_not_an_object"}
        try:  # the frame SocketAPI.call writes (control/client.py), encoded as the transport encodes it
            encode_frame({"op": "control", "method": method, "params": dict(params or {})})
        except FrameError as exc:
            return {"code": UNAVAILABLE, "delivered": False, "detail": f"request:{exc}"}
        except (ValueError, TypeError) as exc:  # a string UTF-8 cannot carry (a lone surrogate), a value JSON cannot
            return {"code": UNAVAILABLE, "delivered": False, "detail": f"request:frame_unencodable:{type(exc).__name__}"}
        return self._api.call(method, params)


def check(environ: Mapping[str, str] | None = None, *,
          make_api: Callable[[str], Any] = CheckedSocketAPI) -> Any:
    """All five steps; returns the API the server forwards to, or raises ``SocketCheckFailed``."""

    path = locate(environ)
    check_path(path)
    api = make_api(path)
    answer = api.call(PROBE[0], dict(PROBE[1]))
    code = answer.get("code") if isinstance(answer, dict) else None
    if not isinstance(code, str) or code == UNAVAILABLE:
        detail = answer.get("detail") if isinstance(answer, dict) else None
        raise SocketCheckFailed("daemon_unreachable", str(detail) if detail else "no control answer")
    return api
