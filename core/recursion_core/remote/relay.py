"""The relay: what ``ssh <host> recursiond relay`` runs on the daemon's machine (lane REMOTE).

sshd runs it as the owner, through the forced command in the owner's authorized_keys (README.md), with
stdin and stdout tied to the SSH channel. It reads one frame at a time on stdin, forwards it on a fresh
connection to the daemon's owner-only Unix socket, and writes the daemon's reply on stdout. It opens no
socket but that one and listens on nothing, so the daemon keeps no network face (CX-026): remote control
is an SSH login as the owner, and the relay is only a pipe from that login to the socket.

What it checks:

* The socket path is an absolute AF_UNIX path. A network-shaped address (``host:port``, a URL, an IP, a
  tuple) raises ``RemoteBindRefused`` (``control_transport.unix.refuse_remote_bind``), at start.
* The socket's directory, when it exists, is a real directory owned by this uid with mode exactly 0700;
  anything else is refused at start and again before every connection. The relay never creates or
  repairs it: a wrong directory means the wrong place. A missing one means the daemon is not running, and
  each request is answered "not delivered".
* The socket server's uid is this uid (``UnixControlClient``'s SO_PEERCRED check).
* Under a forced command, sshd puts the client's own command in ``SSH_ORIGINAL_COMMAND``; anything but
  ``recursiond relay`` is refused, so that a key kept for the relay cannot be used for a shell, scp or
  anything else through it.
* A frame that was readable but cannot be framed again (too large, a lone surrogate, NaN or Infinity,
  all of which ``json.loads`` accepts) is refused ``bad_frame``, not delivered, before anything is sent.

One connection per frame, as the CLI makes one connection per call (``control.client.SocketAPI``): the
relay holds no slot of the socket between requests, and a ``run.stop`` arriving while the socket's
ordinary slots are taken is served on its reserved Stop slot exactly as a local Stop is. Frames are
handled in order, one at a time, per SSH session; the client opens another session for a Stop
(``ssh_transport.RelayAPI``), so a Stop never waits behind a slow call in the same session.

Command ids pass through untouched: the frame and everything in its ``params`` (a keyed method's
``command_id`` included) reach the socket exactly as the client sent them. A reply from the daemon is
passed back unchanged, except that an error claiming the relay's own layer is rewritten (``codec``).

The relay's label (``relay_hello``: SSH or plain stdio, the key label the owner wrote into the forced
command, the client address sshd reports) is evidence for the operator, never authority. Any same-uid
process can open the socket and claim anything in a frame, so the daemon must not trust a label inside
one; the relay therefore puts none there. Telling a relayed request from a local one is a question for
the daemon's own admission (lane CONTROL's scopes), not for this file.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import stat
import sys
from collections.abc import Callable, Mapping
from typing import Any, BinaryIO, TextIO

from recursion_core.control.client import DEFAULT_TIMEOUT_S, socket_path as default_socket_path
from recursion_core.control_transport.errors import (FrameError, InsecureSocketPath, PeerNotAdmitted,
                                                     PlatformNotSupported, RemoteBindRefused, TransportBusy,
                                                     TransportRefused)
from recursion_core.control_transport.framing import MAX_FRAME_BYTES
from recursion_core.control_transport.unix import UnixControlClient, refuse_remote_bind, require_unix_transport

from .codec import (BUSY_MESSAGE, HELLO_OP, NOT_ADMITTED_MESSAGE, RELAY_OP_PREFIX, RELAY_VERSION, UNAVAILABLE,
                    Message, copyable_id, daemon_reply, frame_refusal, read_message, relay_error, write_message)

# What the client asks sshd to run; under a forced command it arrives as SSH_ORIGINAL_COMMAND.
RELAY_COMMAND = "recursiond relay"
EXIT_OK, EXIT_ERROR, EXIT_REFUSED, EXIT_STREAM = 0, 1, 2, 3
SOCKET_DIRECTORY_MODE = 0o700
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

Connect = Callable[..., Any]


def check_socket_directory(directory: str, *, uid: int | None = None) -> None:
    """Refuse a socket directory that is not a real directory, owned by ``uid`` (this uid), of mode 0700.

    ``FileNotFoundError`` when it does not exist. It is checked, never created or repaired.
    """
    info = os.lstat(directory)  # lstat: a symlink is not a directory here, wherever it points
    if not stat.S_ISDIR(info.st_mode):
        raise InsecureSocketPath(f"{directory} is not a real directory")
    owner = os.geteuid() if uid is None else uid
    if info.st_uid != owner:
        raise InsecureSocketPath(f"{directory} is owned by uid {info.st_uid}, not {owner}")
    if stat.S_IMODE(info.st_mode) != SOCKET_DIRECTORY_MODE:
        raise InsecureSocketPath(f"{directory} has mode {stat.S_IMODE(info.st_mode):04o}, want 0700")


def check_key_label(label: object) -> str | None:
    """The owner's name for the key, from the forced command: letters, digits, ``.``, ``_``, ``-``."""
    if label is None:
        return None
    if not isinstance(label, str) or not _LABEL.fullmatch(label):
        raise ValueError("a key label is 1 to 64 letters, digits, '.', '_' or '-', starting with a letter or digit")
    return label


def _client_address(environ: Mapping[str, str]) -> str | None:
    """The client address sshd reports in ``SSH_CONNECTION`` (client ip, client port, server ip, server port)."""
    fields = environ.get("SSH_CONNECTION", "").split()
    if len(fields) != 4:
        return None
    try:
        return str(ipaddress.ip_address(fields[0]))
    except ValueError:
        return None


def _positive_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        raise ValueError(f"{name} must be a positive number")
    return value


class Relay:
    """Forward frames to the owner-only socket at ``socket_path`` (default: the daemon's, from ``environ``).

    ``connect(path, timeout_s=...)`` makes the client for one request (``UnixControlClient``; tests inject
    a fake). ``uid`` overrides the expected owner of the socket directory (tests).
    """

    def __init__(
        self,
        socket_path: object = None,
        *,
        environ: Mapping[str, str] | None = None,
        key_label: str | None = None,
        connect: Connect = UnixControlClient,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_frame_bytes: int = MAX_FRAME_BYTES,
        uid: int | None = None,
    ) -> None:
        environ = dict(os.environ if environ is None else environ)
        path = default_socket_path(environ) if socket_path is None else socket_path
        self.socket_path = refuse_remote_bind(path)  # a network-shaped address is refused here, before anything
        require_unix_transport()
        if not callable(connect):
            raise ValueError("connect must make a client")
        self._timeout_s = _positive_number(timeout_s, "timeout_s")
        if type(max_frame_bytes) is not int or max_frame_bytes < 1:
            raise ValueError("max_frame_bytes must be a positive integer")
        self.directory = os.path.dirname(self.socket_path)
        self._uid = os.geteuid() if uid is None else uid
        try:
            check_socket_directory(self.directory, uid=self._uid)
        except FileNotFoundError:
            pass  # the daemon is not running yet: each request is answered "not delivered"
        self.key_label = check_key_label(key_label)
        self.client_address = _client_address(environ)
        self.transport = "ssh" if self.client_address is not None else "stdio"
        self._connect = connect
        self._max_frame_bytes = max_frame_bytes

    def hello(self, request: Message | None = None) -> Message:
        """The relay's own answer to ``relay_hello``: its version and its label (evidence, not authority)."""
        reply: Message = {
            "ok": True,
            "op": HELLO_OP,
            "relay": {
                "version": RELAY_VERSION,
                "transport": self.transport,
                "key_label": self.key_label,
                "client_address": self.client_address,
            },
        }
        has_id, value = copyable_id(request)
        if has_id:
            reply["id"] = value
        return reply

    def handle(self, request: Message) -> Message:
        """One frame in, one frame out. Never raises: a request that is not an object is refused unsent
        (``bad_frame``); anything unexpected after that is "delivery unknown"."""
        return self._handle(request)[0]

    def _handle(self, request: Message) -> tuple[Message, bool]:
        """The answer, and whether the request may have reached the daemon (the socket was connected)."""
        if not isinstance(request, dict):
            return relay_error("bad_frame", delivered=False, detail="frame_not_object"), False
        try:
            op = request.get("op")
            if isinstance(op, str) and op.startswith(RELAY_OP_PREFIX):
                if op == HELLO_OP:
                    return self.hello(request), False
                return relay_error("unknown_relay_op", delivered=False, detail=op[:64], request=request), False
            return self._forward(request)
        except Exception as exc:  # noqa: BLE001 - a relay that fails must still answer, and truthfully
            return relay_error("relay_internal_error", delivered="unknown", detail=type(exc).__name__,
                               request=request), True

    def _forward(self, request: Message) -> tuple[Message, bool]:
        refusal = frame_refusal(request, max_bytes=self._max_frame_bytes)
        if refusal is not None:
            return relay_error("bad_frame", delivered=False, detail=refusal, request=request), False
        try:
            check_socket_directory(self.directory, uid=self._uid)
            client = self._connect(self.socket_path, timeout_s=self._timeout_s)
        except (OSError, TransportRefused) as exc:
            # Nothing was written: the daemon is not running, its directory is wrong, or the peer is not ours.
            return relay_error(UNAVAILABLE, delivered=False, detail=f"connect:{type(exc).__name__}",
                               request=request), False
        try:
            reply = client.request(request)
        except TransportBusy:
            return dict(BUSY_MESSAGE), False  # exactly the socket's frame: "not delivered, busy"
        except PeerNotAdmitted:
            return dict(NOT_ADMITTED_MESSAGE), False
        except (OSError, TransportRefused) as exc:
            # The frame may have been written and acted on: never report it as not delivered.
            return relay_error(UNAVAILABLE, delivered="unknown", detail=f"request:{type(exc).__name__}",
                               request=request), True
        finally:
            client.close()
        if not isinstance(reply, dict):
            return relay_error(UNAVAILABLE, delivered="unknown", detail="request:invalid_reply", request=request), True
        return daemon_reply(reply), True

    def serve(self, stdin: BinaryIO, stdout: BinaryIO) -> int:
        """Relay frames until stdin ends (``EXIT_OK``) or the stream breaks (``EXIT_STREAM``)."""
        while True:
            try:
                request = read_message(stdin, max_bytes=self._max_frame_bytes)
            except FrameError as exc:
                # A frame that cannot be read leaves no trustworthy boundary for the next: answer once, end.
                self._write_quietly(stdout, relay_error("bad_frame", delivered=False, detail=str(exc)))
                return EXIT_STREAM
            except OSError:
                return EXIT_STREAM
            if request is None:
                return EXIT_OK
            reply, reached = self._handle(request)
            try:
                write_message(stdout, reply, max_bytes=self._max_frame_bytes)
            except (FrameError, ValueError) as exc:
                # The answer cannot be framed. If the daemon had the request, it may have carried it out.
                code = "response_too_large" if isinstance(exc, FrameError) else "response_not_encodable"
                if not self._write_quietly(stdout, relay_error(code, delivered=True if reached else False,
                                                               request=request)):
                    return EXIT_STREAM
            except OSError:
                return EXIT_STREAM

    @staticmethod
    def _write_quietly(stdout: BinaryIO, message: Message) -> bool:
        try:
            write_message(stdout, message)
        except (OSError, ValueError):
            return False
        return True


class _Usage(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # a usage error returns a code; argparse would print and exit the process
        raise _Usage(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="recursiond relay", add_help=False)
    parser.add_argument("command", choices=["relay"])
    parser.add_argument("--socket", dest="socket", default=None)
    parser.add_argument("--key-label", dest="key_label", default=None)
    return parser


def main(
    argv: list[str],
    *,
    environ: Mapping[str, str] | None = None,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    stderr: TextIO | None = None,
    connect: Connect = UnixControlClient,
) -> int:
    """``relay [--socket PATH] [--key-label NAME]``: serve stdin and stdout until stdin ends.

    Nothing but frames is ever written to stdout; refusals go to stderr, which sshd hands to the client.
    """
    environ = dict(os.environ if environ is None else environ)
    err = sys.stderr if stderr is None else stderr
    try:
        args = _parser().parse_args(argv)
    except _Usage as exc:
        print(f"recursion relay: usage error: {exc}", file=err)
        return EXIT_REFUSED
    original = environ.get("SSH_ORIGINAL_COMMAND")
    if original is not None and original.strip() != RELAY_COMMAND:
        print("recursion relay: refused: this key runs only the relay", file=err)
        return EXIT_REFUSED
    try:
        relay = Relay(args.socket, environ=environ, key_label=args.key_label, connect=connect)
    except (RemoteBindRefused, InsecureSocketPath, PlatformNotSupported, ValueError, OSError) as exc:
        # OSError: a socket directory that cannot be looked at (its parent is a file, or not searchable).
        print(f"recursion relay: refused: {type(exc).__name__}: {exc}", file=err)
        return EXIT_REFUSED
    return relay.serve(sys.stdin.buffer if stdin is None else stdin, sys.stdout.buffer if stdout is None else stdout)
