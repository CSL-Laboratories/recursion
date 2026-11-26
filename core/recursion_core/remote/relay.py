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
  The refusal says what was limited and when it frees: the cap, the window, the scope and the seconds
  until the next frame can go (``retry_after_s``), and a sentence that says what the person can do.
* A request whose kind is to END something (``rights.ends_something``: ``run.stop``, ``session.stop``,
  ``session.interrupt``, ``queue.cancel``, and ``approval.answer`` with ``answer`` ``deny``) is never
  counted: once the rights cover it and it fits a frame, it is sent to the daemon at once and answered
  only by the daemon's own answer to it. It takes no slot, reads and writes no window file, waits on no
  file and leaves nothing behind, so a key whose own watching used up its frames can still stop what it
  watches, and a window that cannot be read, a folder changed while the relay runs or a full disk never
  holds it back. Two such requests at once each ask the daemon.
* NO WAIT WITHOUT END ON A FILE: an ordinary frame waits for the lock of its key's window and of the record
  at most ``lock_wait_s`` seconds in all (a setting with a first value, the block below; ``0`` asks once),
  and asks for each lock again at least every ``lock_try_s`` seconds, so that the sessions of one key at
  once never hold each other near the bound. A lock another session or program held each time it was
  asked for then refuses the frame (``rate_limited``, detail
  ``window_busy``, not delivered) with words, or leaves its refusal out of the record (its line still goes
  to ``record``), and the session reads its next frame. Frames of one session are served in order, so a
  request of the ending kind sent in the same session behind ordinary frames waits for them: for each, at
  most ``lock_wait_s`` on files and, when that frame is sent, the daemon's reply to it (``timeout_s``). One
  sent in a session of its own waits on no file at all.
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
handled in order, one at a time, per SSH session; the client opens another session for ``run.stop``
(``ssh_transport.RelayAPI``), so that Stop never waits behind a call in the same session. The daemon keeps
its reserved slots for ``run.stop`` alone (``daemon.control_adapter.is_stop_request``): the other requests
of the ending kind take an ordinary slot, and requests of the ending kind are not counted here, so one key
sending them without pause can hold every slot of the socket, the reserved ones included, while it sends;
a Stop from the daemon's machine or another device is answered busy, not delivered, meanwhile.

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
from recursion_core.control_transport.framing import HEADER, MAX_FRAME_BYTES, encode_frame
from recursion_core.control_transport.unix import UnixControlClient, refuse_remote_bind, require_unix_transport

from .codec import (BUSY_MESSAGE, DELIVERED_VALUES, HELLO_OP, NOT_ADMITTED_MESSAGE, RELAY_LAYER, RELAY_OP_PREFIX,
                    RELAY_VERSION, UNAVAILABLE, Message, RefusedFrame, ascii_text, copyable_id, daemon_reply, fitted,
                    frame_refusal, read_message, relay_error, relay_error_of, write_message)
from .pairing import DEVICE_SCOPES, PairingRefused, device_scopes
from .relay_store import KeyWindow, RefusalRecord, relay_folder
from .rights import NEEDS_ALL, ends_something, key_rights, needs

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
# How long an ordinary frame waits in all for the lock of its key's window and of the record, kept by
# another session of the key or another program, before it is refused (--lock-wait-s S; 0 asks once). A
# session never waits without end on a file, so a Stop behind that frame in the same session is reached
# within this bound; a value with no end (inf, none) or above RELAY_LOCK_WAIT_MOST_S is refused at the
# start. Sessions of one key hold a lock for the time of one read and write of a small file.
RELAY_LOCK_WAIT_S = 2.0
# The longest pause between two tries for one of those locks (--lock-try-s S, more than 0). The pause starts
# at the monotonic clock's resolution and doubles up to this ceiling, so a session asks often enough to take
# a lock that the other sessions of its key each hold for one read and write; with no ceiling but the bound
# a waiter's tries grew far apart, and it lost the lock to those sessions for up to the whole bound.
RELAY_LOCK_TRY_S = 0.001
# The most --lock-wait-s and --lock-try-s may be set to. A wait on a file longer than an hour is one no
# person means: a Stop sent behind that frame in the same session would wait as long. A larger value, one
# too large to be a number of seconds, or a negative zero is refused at the start with words.
RELAY_LOCK_WAIT_MOST_S = 3600.0
UNNAMED_KEY = "unnamed"
# A frame the relay may read (a --max-frame-bytes above the socket's frame) that the socket client refuses
# before a byte is sent: refused here, not delivered.
SOCKET_FRAME_TOO_LARGE = "frame_too_large_for_socket"
# The codes of the relay's own answers. The least frame size the relay can be set to is the body of the
# longest of them with only its code, layer and delivered word: a figure of the frame's format, derived here,
# not a person's setting. Below it no answer could be written within the limit, so such a size is refused
# at the start; at or above it no frame the relay writes is ever above its own limit (``fitted``).
RELAY_CODES = ("bad_frame", "unknown_relay_op", "scope_refused", "relay_internal_error", "rate_limited", UNAVAILABLE,
               "response_too_large", "response_not_encodable")
RELAY_LEAST_FRAME_BYTES = max(
    len(encode_frame({"ok": False, "error": {"code": code, "layer": RELAY_LAYER, "delivered": delivered}}))
    - HEADER.size for code in RELAY_CODES for delivered in DELIVERED_VALUES)

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


def _shown(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 40 else f"{text[:20]}... ({len(text)} characters)"


def _lock_seconds(value: object, name: str, option: str, *, zero: bool, says: str) -> float:
    """Seconds for a wait of the relay on its files: from 0 (above 0 unless ``zero``) to
    ``RELAY_LOCK_WAIT_MOST_S``; a bool, a word, a value that is not finite or too large, and a negative zero
    are refused with words."""
    span = f"from 0 to {RELAY_LOCK_WAIT_MOST_S:g}" if zero else f"above 0 and at most {RELAY_LOCK_WAIT_MOST_S:g}"
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not 0 <= value <= RELAY_LOCK_WAIT_MOST_S or (value == 0 and not zero):
        # int against float compares exactly: a whole number too large for a float is refused, not raised.
        raise ValueError(f"{name} ({option}) {_shown(value)} is not a number of seconds {span}: {says}")
    if math.copysign(1.0, value) < 0:
        raise ValueError(f"{name} ({option}) {_shown(value)} is a negative zero, which the relay does not take "
                         f"as 0: write 0. {says}")
    return float(value)


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


def _seconds_text(seconds: float) -> str:
    """A number of seconds as a person reads it: never an exponent, no trailing zeros."""
    return f"{seconds:f}".rstrip("0").rstrip(".")


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
    ``control.client.state_directory(environ)``). ``lock_wait_s`` bounds, in all, an ordinary frame's wait
    for the locks of the window and the record (seconds, ``0`` to ``RELAY_LOCK_WAIT_MOST_S``);
    ``lock_try_s`` is the longest pause between two tries for one of them (seconds, above ``0``).
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
        lock_wait_s: float = RELAY_LOCK_WAIT_S,
        lock_try_s: float = RELAY_LOCK_TRY_S,
    ) -> None:
        environ = dict(os.environ if environ is None else environ)
        path = default_socket_path(environ) if socket_path is None else socket_path
        self.socket_path = refuse_remote_bind(path)  # a network-shaped address is refused here, before anything
        require_unix_transport()
        if not callable(connect):
            raise ValueError("connect must make a client")
        self._timeout_s = _positive_number(timeout_s, "timeout_s")
        if not math.isfinite(timeout_s) or timeout_s > threading.TIMEOUT_MAX:
            # The socket's wait for a reply cannot be set longer (a figure of the platform, not of ours):
            # such a value would fail at the first request, a Stop included, so it is refused here.
            raise ValueError(f"timeout_s {timeout_s!r} is longer than this system can wait for a reply "
                             f"({threading.TIMEOUT_MAX:.0f} seconds): set a finite number of seconds up to that")
        if max_frame_bytes is not None and (type(max_frame_bytes) is not int or max_frame_bytes < 1):
            raise ValueError("max_frame_bytes must be a positive integer, or None for no limit")
        if max_frame_bytes is not None and max_frame_bytes < RELAY_LEAST_FRAME_BYTES:
            raise ValueError(f"max_frame_bytes (--max-frame-bytes) {max_frame_bytes} is below "
                             f"{RELAY_LEAST_FRAME_BYTES}, the bytes the relay's shortest answer takes: set "
                             f"{RELAY_LEAST_FRAME_BYTES} or more, or none for no limit of the relay's own")
        self.lock_wait_s = _lock_seconds(
            lock_wait_s, "lock_wait_s", "--lock-wait-s", zero=True,
            says=f"a relay session never waits without end on a file, and a wait longer than "
                 f"{RELAY_LOCK_WAIT_MOST_S:g} seconds is one no person means, since a Stop behind that frame "
                 f"in the same session would wait as long; set how long an ordinary request may wait for the "
                 f"relay's files (0 asks once)")
        self.lock_try_s = _lock_seconds(
            lock_try_s, "lock_try_s", "--lock-try-s", zero=False,
            says="it is the longest pause between two tries for one of the relay's files (0 would ask without "
                 "a pause and keep a processor busy); set a short pause, about a thousandth of a second")
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
                self._key_window = KeyWindow(self.state_folder, self.key, uid=self._uid)
            if self.record_max != 0:
                self._refusal_record = RefusalRecord(self.state_folder, self.record_max, uid=self._uid)

    def grant(self) -> dict:
        """What this key reaches, for status: the device, its rights and the caps (``None`` is no limit)."""
        return {"device": self.device_id, "rights": list(self.rights),
                "caps": {"max_frames": self.max_frames, "window_s": self.window_s,
                         "max_frame_bytes": self.max_frame_bytes}}

    def _left(self, deadline: float) -> float:
        """The seconds of an ordinary frame's wait on files still left before ``deadline`` (0 when none)."""
        return max(0.0, deadline - time.monotonic())

    def _note(self, event: dict, *, wait_s: float) -> str | None:
        """Write ``event`` to the record and tell it to ``record``. Returns the name of what kept it out of the
        record on the daemon's machine, else ``None``. The record's lock is waited for at most ``wait_s``
        seconds; ``0`` asks once (a refusal of the ending kind waits on no file)."""
        missed = None
        if self._refusal_record is not None:
            try:
                self._refusal_record.add({**event, "at": time.time(), "key": self.key, "device": self.device_id,
                                          "key_label": self.key_label, "client_address": self.client_address},
                                         wait_s=wait_s, try_s=self.lock_try_s)
            except Exception as exc:  # noqa: BLE001 - a record that fails never changes an answer
                missed = type(exc).__name__
        if self._record is not None:
            try:
                self._record(event)
            except Exception:  # noqa: BLE001 - a record that fails never changes an answer
                pass
        return missed

    def _refused(self, request: Message, event: dict, code: str, deadline: float, **error: Any) -> Message:
        """A refusal of the relay's own, noted. For a request of the ending kind the record is not waited for,
        and when it could not be written the answer says so in one sentence (``record_notice``); any other
        waits for it until ``deadline`` at most."""
        ending = ends_something(request)
        missed = self._note(event, wait_s=0.0 if ending else self._left(deadline))
        reply = relay_error(code, delivered=False, request=request, **error)
        if ending and missed is not None:
            reply["error"]["record_notice"] = (
                f"This refusal is not in the relay's record of refusals on the daemon's machine: the record "
                f"could not be written ({missed}). The refusal itself stands as answered here.")
        return reply

    def _take_slot(self, deadline: float) -> tuple[int, float | None] | None:
        """Let one frame through the window and return ``None``, or return how many were already in it and
        the seconds until the oldest of them leaves (``None`` when none is in it: a cap of 0 never frees).

        Never asked for a request of the ending kind. ``OSError`` when the key's window on the daemon's
        machine cannot be read or written; ``BlockingIOError`` (``relay_store.RelayFileBusy``) when its lock
        is still held at ``deadline``."""
        if self.max_frames is None:
            return None
        with self._window_lock:
            # The key's window reads the clock itself, under the lock its sessions share (KeyWindow.take).
            if self._key_window is not None:
                return self._key_window.take(self._clock, self.max_frames, self.window_s,
                                             wait_s=self._left(deadline), try_s=self.lock_try_s)
            now = self._clock()
            while self._forwarded and self._forwarded[0] <= now - self.window_s:
                self._forwarded.popleft()
            if len(self._forwarded) >= self.max_frames:
                return len(self._forwarded), (self._forwarded[0] + self.window_s - now if self._forwarded else None)
            self._forwarded.append(now)
            return None

    def _limited(self, request: Message, retry_after_s: float | None, *, unreadable: str | None = None,
                 busy: bool = False) -> Message:
        """The ``rate_limited`` answer: what was limited, when it frees, and what the person can do."""
        whose = "this key" if self.cap_scope == "key" else "this session"
        cap = (f"{self.max_frames} request{'' if self.max_frames == 1 else 's'} in any "
               f"{_seconds_text(self.window_s)} second{'' if self.window_s == 1 else 's'}")
        never = "A Stop, an interrupt, a cancel or a denied approval is never held by this limit."
        if busy:
            wait = _seconds_text(self.lock_wait_s)
            detail = f"window_busy:lock_wait_s:{self.lock_wait_s:g}"
            if self.lock_wait_s == 0:
                asked = (f"The relay asked once for {whose}'s count of requests on the daemon's machine "
                         f"(--lock-wait-s 0: this request may wait 0 seconds for it), and another session or "
                         f"program held it then")
            else:
                asked = (f"The relay asked for {whose}'s count of requests on the daemon's machine again and "
                         f"again over the {wait} second{'' if wait == '1' else 's'} this request may wait for it "
                         f"(--lock-wait-s {wait}), and another session or program held it each time it asked")
            words = (f"{asked}, so the relay did not send this request and went on with the next one: it never "
                     f"waits longer on a file. Try again; if this goes on, look for a program that holds the "
                     f"relay's files under the daemon's state folder there. A Stop, an interrupt, a cancel or a "
                     f"denied approval is still sent to the daemon at once and is never counted here.")
        elif unreadable is not None:
            detail = f"window_unreadable:{unreadable}"
            words = (f"The relay could not read {whose}'s count of requests on the daemon's machine ({unreadable}), "
                     f"so it cannot show that this request is within its limit of {cap}, and did not send it. Try "
                     f"again; if this goes on, look at the relay's folder under the daemon's state folder there. A "
                     f"Stop, an interrupt, a cancel or a denied approval still goes: it is never counted.")
        else:
            detail = f"max_frames:{self.max_frames}:window_s:{self.window_s:g}"
            limit = f"The relay lets {whose} send {cap} (--max-frames {self.max_frames}, --window-s " \
                    f"{_seconds_text(self.window_s)})"
            raise_it = "whoever set up this key on the daemon's machine raises --max-frames in its forced command"
            if retry_after_s is None:
                words = f"{limit}, so no other request can go until {raise_it}. {never}"
            else:
                wait = _seconds_text(math.ceil(round(retry_after_s * 10, 6)) / 10)
                words = (f"{limit}, and that many were sent: the next can go in {wait} second"
                         f"{'' if wait == '1' else 's'}. Wait that long, or ask that {raise_it}. {never}")
        reply = relay_error("rate_limited", delivered=False, detail=detail, request=request)
        reply["error"].update({"cap": "max_frames", "max_frames": self.max_frames, "window_s": self.window_s,
                               "cap_scope": self.cap_scope, "retry_after_s": retry_after_s, "message": words})
        return reply

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

    def _handle(self, request: Message) -> tuple[Message, bool | str]:
        """The answer, and what is known of the request's delivery, in the words ``delivered`` takes:
        ``True`` only when a reply of the daemon was read for it, ``"unknown"`` when it may have reached the
        daemon and no reply was read, ``False`` when nothing was sent."""
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
            # The most this frame may wait in all for the relay's files: past it, the next frame is read.
            deadline = time.monotonic() + self.lock_wait_s
            missing = needs(self.rights, request)
            if missing is not None:
                # The relay's own lock: refused here, before the frame can reach the daemon.
                name, right = missing
                detail = "method_not_in_table" if right == NEEDS_ALL else f"needs_{right}"
                return self._refused(request, {"refused": "scope_refused", "method": ascii_text(name, 64),
                                               "needs": right},
                                     "scope_refused", deadline, detail=detail,
                                     fields={"method": name, "needs": right}), False
            return self._forward(request, deadline)
        except Exception as exc:  # noqa: BLE001 - a relay that fails must still answer, and truthfully
            return relay_error("relay_internal_error", delivered="unknown", detail=type(exc).__name__,
                               request=request), "unknown"

    def _forward(self, request: Message, deadline: float) -> tuple[Message, bool | str]:
        refusal = frame_refusal(request, max_bytes=self._max_frame_bytes)
        if refusal is None and self._max_frame_bytes > MAX_FRAME_BYTES and frame_refusal(request) is not None:
            # Over the socket's own frame (a protocol figure): the socket client would refuse it before
            # writing a byte, so it is not delivered, and is said so here rather than "unknown" after a connect.
            refusal = SOCKET_FRAME_TOO_LARGE
        if refusal is not None:
            if refusal == "frame_too_large":
                return self._refused(request, {"refused": "bad_frame", "cap": "max_frame_bytes",
                                               "value": self.max_frame_bytes},
                                     "bad_frame", deadline, detail=refusal), False
            return relay_error("bad_frame", delivered=False, detail=refusal, request=request), False
        if ends_something(request):
            # Never counted: no slot, no window file, no lock, nothing on disk. Sent now; the daemon answers.
            return self._deliver(request)
        try:
            taken = self._take_slot(deadline)
        except BlockingIOError:
            # Held by another session or program for the whole wait (``RelayFileBusy``, or ``BlockingIOError``
            # from the one try of a bound of 0): refused, and the next frame is read.
            self._note({"refused": "rate_limited", "cap": "max_frames", "window": "busy",
                        "lock_wait_s": self.lock_wait_s}, wait_s=self._left(deadline))
            return self._limited(request, None, busy=True), False
        except OSError as exc:
            # The key's window cannot be read: the cap cannot be shown to hold, so the frame is not sent.
            self._note({"refused": "rate_limited", "cap": "max_frames", "window": f"unreadable:{type(exc).__name__}"},
                       wait_s=self._left(deadline))
            return self._limited(request, None, unreadable=type(exc).__name__), False
        if taken is not None:
            seen, retry_after_s = taken
            self._note({"refused": "rate_limited", "cap": "max_frames", "value": self.max_frames,
                        "window_s": self.window_s, "seen": seen}, wait_s=self._left(deadline))
            return self._limited(request, retry_after_s), False
        return self._deliver(request)

    def _deliver(self, request: Message) -> tuple[Message, bool | str]:
        """Send ``request`` on a fresh connection to the socket and pass the daemon's answer back."""
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
                               request=request), "unknown"
        finally:
            client.close()
        if not isinstance(reply, dict):
            return relay_error(UNAVAILABLE, delivered="unknown", detail="request:invalid_reply",
                               request=request), "unknown"
        return daemon_reply(reply), True

    def serve(self, stdin: BinaryIO, stdout: BinaryIO) -> int:
        """Relay frames until stdin ends (``EXIT_OK``) or the stream breaks (``EXIT_STREAM``)."""
        while True:
            try:
                request = read_message(stdin, max_bytes=self._max_frame_bytes)
            except FrameError as exc:
                # A frame that cannot be read leaves no trustworthy boundary for the next: answer once, end.
                if str(exc) == "frame_too_large":
                    self._note({"refused": "bad_frame", "cap": "max_frame_bytes", "value": self.max_frame_bytes},
                               wait_s=self.lock_wait_s)
                self._write_quietly(stdout, relay_error("bad_frame", delivered=False, detail=str(exc)))
                return EXIT_STREAM
            except OSError:
                return EXIT_STREAM
            if request is None:
                return EXIT_OK
            reply, delivered = self._handle(request)
            try:
                write_message(stdout, reply, max_bytes=self._max_frame_bytes)
            except (FrameError, ValueError) as exc:
                if not self._write_quietly(stdout, self._unframed(reply, request, delivered, exc)):
                    return EXIT_STREAM
            except OSError:
                return EXIT_STREAM

    def _unframed(self, reply: Message, request: Message, delivered: bool | str, exc: Exception) -> Message:
        """What is written in place of an answer that cannot be framed.

        The relay's own error stays that error, so that the first reason stands (a refusal stays that
        refusal): ``_write_quietly`` makes it fit. Any other answer becomes ``response_too_large`` (or
        ``response_not_encodable``) with what is known of the request: ``delivered`` true only when the
        daemon's reply was read, ``"unknown"`` when the request may have reached the daemon and no reply was
        read, false when nothing was sent.
        """
        if relay_error_of(reply) is not None:
            return reply
        code = "response_too_large" if isinstance(exc, FrameError) else "response_not_encodable"
        return relay_error(code, delivered=delivered, request=request)

    def _write_quietly(self, stdout: BinaryIO, message: Message) -> bool:
        """Write the relay's own error within the relay's own frame limit (``fitted``); false when the stream
        broke. Its code and delivered word are always written: the limit is never below
        ``RELAY_LEAST_FRAME_BYTES``."""
        answer = fitted(message, max_bytes=self._max_frame_bytes)
        if answer is None:
            return False
        try:
            write_message(stdout, answer, max_bytes=self._max_frame_bytes)
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
    parser.add_argument("--lock-wait-s", dest="lock_wait_s", default=None)
    parser.add_argument("--lock-try-s", dest="lock_try_s", default=None)
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


def _lock_wait_option(text: str | None) -> float:
    if text is None:
        return RELAY_LOCK_WAIT_S
    try:
        return float(text)  # whether it is a finite number of 0 or more is the Relay's own check
    except ValueError:
        raise _Usage("--lock-wait-s is a number of seconds, 0 or more: a relay session never waits without "
                     "end on a file") from None


def _lock_try_option(text: str | None) -> float:
    if text is None:
        return RELAY_LOCK_TRY_S
    try:
        return float(text)  # whether it is above 0 and not too large is the Relay's own check
    except ValueError:
        raise _Usage("--lock-try-s is a number of seconds above 0: the longest pause between two tries for "
                     "one of the relay's files") from None


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
    [--window-s S] [--max-frame-bytes N|none] [--cap-scope key|session] [--record-max N|none]
    [--lock-wait-s S] [--lock-try-s S]``: serve stdin and stdout until stdin ends.

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
        lock_wait_s = _lock_wait_option(args.lock_wait_s)
        lock_try_s = _lock_try_option(args.lock_try_s)
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
                      record_max=record_max, lock_wait_s=lock_wait_s, lock_try_s=lock_try_s,
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
