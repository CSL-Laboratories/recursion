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
  all of which ``json.loads`` accepts) is refused ``bad_frame``, not delivered, before anything is sent,
  and the session goes on with the next frame. Only a frame with no sound boundary (a length over the
  limit, a truncated body, a body that is not one JSON object) ends the session.
* The key's RIGHTS (``rights.py``), in the core's vocabulary: a frame is forwarded only when they cover
  its method, through the relay's one table of method to right; every other frame is refused
  ``scope_refused`` by the relay itself, not delivered, naming the method and the right it needs, before
  anything is sent. Every key holds watch and Stop (``read`` plus ``run.stop``) unless its forced command
  names other rights (``--rights``). A key for a paired device (``--device``) is held to what pairing
  allows (``pairing.device_scopes``); a key that names no device holds more only where the person's own
  forced command names it, all four (``--rights read,run,approve,manage``) included.
* CAPS on the forwarded stream, each a setting with a first value (the block below): at most
  ``max_frames`` frames forwarded in any ``window_s`` seconds (a sliding window on a monotonic clock),
  and a frame of at most ``max_frame_bytes``. The window holds for the KEY (``cap_scope`` ``key``): every
  session of one key shares it, kept on the daemon's machine under a lock (``relay_store``), so that
  opening more sessions lets no more frames through; ``cap_scope`` ``session`` gives each session a
  window of its own. A key is named by its device, else by its label, else it is the one unnamed key.
  Over a cap the frame is refused (``rate_limited`` or ``bad_frame``, not delivered). ``None`` (``none``
  on the command line) is no limit, a value a person may set; a frame above the socket's own frame size
  still cannot be read by the socket, and a length over the cap is refused before its body is read.
* THE RECORD: every refusal the rights and the caps make is written to the record on the daemon's
  machine (``relay/refusals.jsonl`` under the daemon's state folder), at most ``record_max`` lines kept,
  newest last, and is also told to ``record`` (``main`` writes it on stderr, which sshd hands to the
  client, never on stdout).

What waits for the core: the relay connects to the socket as the owner's uid, as before. Until the core
checks rights at its socket (a paired device's identity presented on the connection, and that device's
rights checked there again), the relay's own table is the only lock between a relay key and the daemon.

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
import json
import math
import os
import re
import stat
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from typing import Any, BinaryIO, TextIO

from recursion_core.control.client import DEFAULT_TIMEOUT_S, socket_path as default_socket_path, state_directory
from recursion_core.control_transport.errors import (FrameError, InsecureSocketPath, PeerNotAdmitted,
                                                     PlatformNotSupported, RemoteBindRefused, TransportBusy,
                                                     TransportRefused)
from recursion_core.control_transport.framing import MAX_FRAME_BYTES
from recursion_core.control_transport.unix import UnixControlClient, refuse_remote_bind, require_unix_transport

from .codec import (BUSY_MESSAGE, HELLO_OP, NOT_ADMITTED_MESSAGE, RELAY_OP_PREFIX, RELAY_VERSION, UNAVAILABLE,
                    Message, RefusedFrame, ascii_text, copyable_id, daemon_reply, frame_refusal, read_message,
                    relay_error, write_message)
from .pairing import DEVICE_SCOPES, PairingRefused, device_scopes
from .relay_store import KeyWindow, RefusalRecord, relay_folder
from .rights import NEEDS_ALL, key_rights, needs

# What the client asks sshd to run; under a forced command it arrives as SSH_ORIGINAL_COMMAND.
RELAY_COMMAND = "recursiond relay"
EXIT_OK, EXIT_ERROR, EXIT_REFUSED, EXIT_STREAM = 0, 1, 2, 3
SOCKET_DIRECTORY_MODE = 0o700
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_DEVICE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_WHOLE = re.compile(r"[0-9]{1,18}")

# THE CAPS ON THE FORWARDED STREAM: the one place their first values are named. Each is a setting a person
# changes per key on the forced command (--max-frames, --window-s, --max-frame-bytes; ``none`` is no
# limit) or per Relay; they become rows of the person's settings file when that file is in this tree.
# A run with no cap forwarded 300 frames in 0.12 s; the first values below allow a burst of that size and
# then five frames a second on average. The frame size's first value is the socket's own frame (1 MiB).
# The window holds per key across sessions, or per session (--cap-scope key|session). The record of
# refusals on the daemon's machine keeps at most RELAY_RECORD_MAX lines (--record-max N|none; 0 keeps none).
RELAY_MAX_FRAMES = 300
RELAY_WINDOW_S = 60.0
RELAY_MAX_FRAME_BYTES = MAX_FRAME_BYTES
CAP_SCOPES = ("key", "session")
RELAY_CAP_SCOPE = "key"
RELAY_RECORD_MAX = 1000
UNNAMED_KEY = "unnamed"

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


def _cap(value: object, name: str, least: int) -> int | None:
    """A count cap: ``None`` (no limit) or a whole number of at least ``least``."""
    if value is None:
        return None
    if type(value) is not int or value < least:
        raise ValueError(f"{name} must be a whole number of at least {least}, or None for no limit")
    return value


def check_device_id(device_id: object) -> str | None:
    """A paired device's id as the forced command names it: 1 to 64 letters, digits, ``_`` or ``-``."""
    if device_id is None:
        return None
    if type(device_id) is not str or not _DEVICE.fullmatch(device_id):
        raise ValueError("a device id is 1 to 64 letters, digits, '_' or '-'")
    return device_id


def relay_rights(rights: Iterable[str] | None, device_id: str | None) -> tuple[str, ...]:
    """The rights a relay key holds: watch and Stop (``DEVICE_SCOPES``) unless the forced command names
    others. A device key is held to what pairing allows; a key with no device holds what is named, all
    four included, and nothing more than watch and Stop when nothing is named."""
    if device_id is not None:
        try:
            return device_scopes(DEVICE_SCOPES if rights is None else rights)
        except PairingRefused as exc:
            raise ValueError(f"rights: {exc}") from None
    return key_rights(DEVICE_SCOPES if rights is None else rights)


class Relay:
    """Forward frames to the owner-only socket at ``socket_path`` (default: the daemon's, from ``environ``).

    ``connect(path, timeout_s=...)`` makes the client for one request (``UnixControlClient``; tests inject
    a fake). ``uid`` overrides the expected owner of the socket directory (tests). ``rights`` and
    ``device_id`` are the key's (``relay_rights``). ``max_frames`` per ``window_s`` and ``max_frame_bytes``
    are the caps (``None`` is no limit); ``cap_scope`` says whether the window holds per key (shared by
    every session of the key) or per session; ``clock`` is the window's monotonic clock. Every refusal
    the caps and the rights make is written to the record on the daemon's machine, at most
    ``record_max`` lines (``None``: no limit; ``0``: none), and told to ``record(event)`` (a dict; it may
    not raise into the relay). ``state_dir`` is the daemon's state folder (default:
    ``control.client.state_directory(environ)``).
    """

    def __init__(
        self,
        socket_path: object = None,
        *,
        environ: Mapping[str, str] | None = None,
        key_label: str | None = None,
        connect: Connect = UnixControlClient,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_frame_bytes: int | None = RELAY_MAX_FRAME_BYTES,
        uid: int | None = None,
        rights: Iterable[str] | None = None,
        device_id: str | None = None,
        max_frames: int | None = RELAY_MAX_FRAMES,
        window_s: float = RELAY_WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
        record: Callable[[dict], Any] | None = None,
        cap_scope: str = RELAY_CAP_SCOPE,
        record_max: int | None = RELAY_RECORD_MAX,
        state_dir: str | None = None,
    ) -> None:
        environ = dict(os.environ if environ is None else environ)
        path = default_socket_path(environ) if socket_path is None else socket_path
        self.socket_path = refuse_remote_bind(path)  # a network-shaped address is refused here, before anything
        require_unix_transport()
        if not callable(connect):
            raise ValueError("connect must make a client")
        self._timeout_s = _positive_number(timeout_s, "timeout_s")
        if max_frame_bytes is not None and (type(max_frame_bytes) is not int or max_frame_bytes < 1):
            raise ValueError("max_frame_bytes must be a positive integer, or None for no limit")
        self.max_frames = _cap(max_frames, "max_frames", 0)
        if isinstance(window_s, bool) or not isinstance(window_s, (int, float)) or not math.isfinite(window_s) \
                or not window_s > 0:
            raise ValueError("window_s must be a positive finite number")
        if not callable(clock) or (record is not None and not callable(record)):
            raise ValueError("clock and record must be callable")
        if type(cap_scope) is not str or cap_scope not in CAP_SCOPES:
            raise ValueError("cap_scope must be 'key' or 'session'")
        self.cap_scope = cap_scope
        self.record_max = _cap(record_max, "record_max", 0)
        self.device_id = check_device_id(device_id)
        self.rights = relay_rights(rights, self.device_id)
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
        self.max_frame_bytes = max_frame_bytes
        # None is no limit of the relay's own: the socket's frame (a protocol figure) is still the most it reads.
        self._max_frame_bytes = MAX_FRAME_BYTES if max_frame_bytes is None else max_frame_bytes
        self.window_s = float(window_s)
        self._clock, self._record = clock, record
        self._window_lock = threading.Lock()
        self._forwarded: deque[float] = deque()  # the window of this session (cap_scope "session")
        # The key's name on the daemon's machine: its device, else its label, else the one unnamed key.
        self.key = (f"device-{self.device_id}" if self.device_id is not None
                    else f"label-{self.key_label}" if self.key_label is not None else UNNAMED_KEY)
        self._key_window = self._refusal_record = None
        self.state_folder = None
        if (self.cap_scope == "key" and self.max_frames is not None) or self.record_max != 0:
            self.state_folder = relay_folder(state_directory(environ) if state_dir is None else state_dir,
                                             uid=self._uid)
            if self.cap_scope == "key" and self.max_frames is not None:
                self._key_window = KeyWindow(self.state_folder, self.key)
            if self.record_max != 0:
                self._refusal_record = RefusalRecord(self.state_folder, self.record_max)

    def grant(self) -> dict:
        """What this key reaches, for status: the device, its rights and the caps (``None`` is no limit)."""
        return {"device": self.device_id, "rights": list(self.rights),
                "caps": {"max_frames": self.max_frames, "window_s": self.window_s,
                         "max_frame_bytes": self.max_frame_bytes}}

    def _note(self, event: dict) -> None:
        if self._refusal_record is not None:
            try:
                self._refusal_record.add({**event, "at": time.time(), "key": self.key, "device": self.device_id,
                                          "key_label": self.key_label, "client_address": self.client_address})
            except Exception:  # noqa: BLE001 - a record that fails never changes an answer
                pass
        if self._record is None:
            return
        try:
            self._record(event)
        except Exception:  # noqa: BLE001 - a record that fails never changes an answer
            pass

    def _take_slot(self) -> int | None:
        """Let one frame through the window and return ``None``, or return how many were already in it.

        ``OSError`` when the key's window on the daemon's machine cannot be read or written."""
        if self.max_frames is None:
            return None
        with self._window_lock:
            now = self._clock()
            if self._key_window is not None:
                return self._key_window.take(now, self.max_frames, self.window_s)
            while self._forwarded and self._forwarded[0] <= now - self.window_s:
                self._forwarded.popleft()
            if len(self._forwarded) >= self.max_frames:
                return len(self._forwarded)
            self._forwarded.append(now)
            return None

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
            "grant": self.grant(),
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
            if isinstance(request, RefusedFrame):
                # A body the socket refuses on read: never forwarded, whatever a later duplicate key made of it.
                return relay_error("bad_frame", delivered=False, detail=request.detail, request=request), False
            missing = needs(self.rights, request)
            if missing is not None:
                # The relay's own lock: refused here, before the frame can reach the daemon.
                name, right = missing
                self._note({"refused": "scope_refused", "method": ascii_text(name, 64), "needs": right})
                detail = "method_not_in_table" if right == NEEDS_ALL else f"needs_{right}"
                return relay_error("scope_refused", delivered=False, detail=detail, request=request,
                                   fields={"method": name, "needs": right}), False
            return self._forward(request)
        except Exception as exc:  # noqa: BLE001 - a relay that fails must still answer, and truthfully
            return relay_error("relay_internal_error", delivered="unknown", detail=type(exc).__name__,
                               request=request), True

    def _forward(self, request: Message) -> tuple[Message, bool]:
        refusal = frame_refusal(request, max_bytes=self._max_frame_bytes)
        if refusal is not None:
            if refusal == "frame_too_large":
                self._note({"refused": "bad_frame", "cap": "max_frame_bytes", "value": self.max_frame_bytes})
            return relay_error("bad_frame", delivered=False, detail=refusal, request=request), False
        try:
            seen = self._take_slot()
        except OSError as exc:
            # The key's window cannot be read: the cap cannot be shown to hold, so the frame is not sent.
            self._note({"refused": "rate_limited", "cap": "max_frames", "window": f"unreadable:{type(exc).__name__}"})
            return relay_error("rate_limited", delivered=False, detail=f"window_unreadable:{type(exc).__name__}",
                               request=request), False
        if seen is not None:
            self._note({"refused": "rate_limited", "cap": "max_frames", "value": self.max_frames,
                        "window_s": self.window_s, "seen": seen})
            return relay_error("rate_limited", delivered=False,
                               detail=f"max_frames:{self.max_frames}:window_s:{self.window_s:g}",
                               request=request), False
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
                if str(exc) == "frame_too_large":
                    self._note({"refused": "bad_frame", "cap": "max_frame_bytes", "value": self.max_frame_bytes})
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
    parser.add_argument("--device", dest="device", default=None)
    parser.add_argument("--rights", dest="rights", default=None)
    parser.add_argument("--max-frames", dest="max_frames", default=None)
    parser.add_argument("--window-s", dest="window_s", default=None)
    parser.add_argument("--max-frame-bytes", dest="max_frame_bytes", default=None)
    parser.add_argument("--cap-scope", dest="cap_scope", choices=list(CAP_SCOPES), default=RELAY_CAP_SCOPE)
    parser.add_argument("--record-max", dest="record_max", default=None)
    return parser


def _count_option(text: str | None, name: str, first: int | None) -> int | None:
    """``none`` is no limit; a whole number is the cap; nothing given is the first value."""
    if text is None:
        return first
    if text == "none":
        return None
    if not _WHOLE.fullmatch(text):
        raise _Usage(f"{name} is a whole number or none")
    return int(text)


def _window_option(text: str | None) -> float:
    if text is None:
        return RELAY_WINDOW_S
    try:
        return float(text)  # whether it is a positive finite number is the Relay's own check
    except ValueError:
        raise _Usage("--window-s is a number of seconds") from None


def _rights_option(text: str | None) -> list[str] | None:
    return None if text is None else text.split(",")


def main(
    argv: list[str],
    *,
    environ: Mapping[str, str] | None = None,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    stderr: TextIO | None = None,
    connect: Connect = UnixControlClient,
) -> int:
    """``relay [--socket PATH] [--key-label NAME] [--device ID] [--rights LIST] [--max-frames N|none]
    [--window-s S] [--max-frame-bytes N|none] [--cap-scope key|session] [--record-max N|none]``: serve
    stdin and stdout until stdin ends.

    Nothing but frames is ever written to stdout. Every frame the caps or the rights refused is written
    to the record on the daemon's machine, and one line each goes to stderr, which sshd hands to the
    client; so do the refusals of the start.
    """
    environ = dict(os.environ if environ is None else environ)
    err = sys.stderr if stderr is None else stderr
    try:
        args = _parser().parse_args(argv)
        max_frames = _count_option(args.max_frames, "--max-frames", RELAY_MAX_FRAMES)
        max_frame_bytes = _count_option(args.max_frame_bytes, "--max-frame-bytes", RELAY_MAX_FRAME_BYTES)
        window_s = _window_option(args.window_s)
        record_max = _count_option(args.record_max, "--record-max", RELAY_RECORD_MAX)
    except _Usage as exc:
        print(f"recursion relay: usage error: {exc}", file=err)
        return EXIT_REFUSED
    original = environ.get("SSH_ORIGINAL_COMMAND")
    if original is not None and original.strip() != RELAY_COMMAND:
        print("recursion relay: refused: this key runs only the relay", file=err)
        return EXIT_REFUSED
    try:
        relay = Relay(args.socket, environ=environ, key_label=args.key_label, connect=connect,
                      device_id=args.device, rights=_rights_option(args.rights), max_frames=max_frames,
                      window_s=window_s, max_frame_bytes=max_frame_bytes, cap_scope=args.cap_scope,
                      record_max=record_max,
                      record=lambda event: print(f"recursion relay: record: {json.dumps(event, sort_keys=True)}",
                                                 file=err, flush=True))
    except (RemoteBindRefused, InsecureSocketPath, PlatformNotSupported, ValueError, OSError) as exc:
        # OSError: a socket directory that cannot be looked at (its parent is a file, or not searchable).
        print(f"recursion relay: refused: {type(exc).__name__}: {exc}", file=err)
        return EXIT_REFUSED
    for name, value in (("max_frames", relay.max_frames), ("max_frame_bytes", relay.max_frame_bytes),
                        ("record_max", relay.record_max)):
        if value is None:
            print(f"recursion relay: note: {name} is none: no limit", file=err, flush=True)
    return relay.serve(sys.stdin.buffer if stdin is None else stdin, sys.stdout.buffer if stdout is None else stdout)
