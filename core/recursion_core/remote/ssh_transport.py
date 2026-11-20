"""The SSH client transport (lane REMOTE): drive a daemon on another machine through its relay.

``RelayAPI(target).call(method, params)`` has the shape of ``ControlAPI.call`` and gives the answers the
CLI's socket client gives (``control.client.SocketAPI``), so the CLI, the TUI and the desktop app can use
either one unchanged. It sends the socket client's own frame, ``{"op": "control", "method", "params"}``,
with nothing added. Underneath, ``SshRelayClient`` runs ``ssh ... -- <host> recursiond relay`` and speaks
the relay's frames (``codec``) on the child's stdin and stdout.

Delivery is reported as the socket client reports it, with one more hop to account for:

* ``delivered: false``: nothing reached the daemon. ssh could not start or log in, the host key is not
  the pinned one, the relay did not answer its hello, the request could not be written whole in time,
  the daemon was not running behind the relay, or the socket was busy or did not admit the relay.
  Details begin ``connect:`` (before any request was written), ``request:`` (the request was not written
  whole), ``relay:`` (the relay's own answer) or are the socket's own words (``busy``,
  ``daemon:scope_refused``).
* ``delivered: "unknown"``: the request may have been written whole and no complete answer came back (the
  SSH link or the relay died, the answer did not arrive in time, or the write's own outcome is not known).
  It may have been carried out: read the run's status before retrying. Nothing is ever resent.
* Otherwise the daemon's answer, verbatim.

The request's deadline covers writing it and waiting for the answer; each wait gets only what is left of
it. A write that does not finish in time ends the session (the ssh child is killed). The request is "not
delivered" only when the write failed with bytes still unsent, since the relay acts only on whole frames;
when the writer cannot show that (every byte may have gone out), it is ``"unknown"``. ``close()`` ends a
session at once, also while a request is blocked in it.

The host key is pinned, not trusted on first use. ``SshTarget.known_hosts`` names one known_hosts file
that must accept exactly one key for the host, the expected one (``host_key_fingerprint``), and must hold
no certificate authority for it, since ssh would accept any host certificate that authority signed. ssh
then runs against that file only, with ``StrictHostKeyChecking=yes`` and ``-F none``: no configuration
file is read, so nothing can add a key source (``KnownHostsCommand``) or change the name looked up
(``HostKeyAlias``). Agent and X11 forwarding, port forwarding, a TTY, connection sharing and local
commands are all off. The relay's hello gives a label (``SshRelayClient.label``) for the operator to see;
it is evidence, not authority.

A Stop never waits behind another call: ``RelayAPI`` opens a fresh SSH session for each call, as the
socket client makes one connection per call. With ``persistent=True`` ordinary calls share one session
(for a surface that polls), and ``run.stop`` still gets a session of its own, without waiting for the
shared one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import queue
import re
import struct
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from recursion_core.control.client import DEFAULT_TIMEOUT_S as SOCKET_TIMEOUT_S
from recursion_core.control_transport.errors import PeerNotAdmitted, TransportBusy, TransportRefused
from recursion_core.control_transport.framing import MAX_FRAME_BYTES, encode_frame

from .codec import (BUSY_MESSAGE, HELLO_OP, NOT_ADMITTED_MESSAGE, RELAY_VERSION, UNAVAILABLE, Message, frame_refusal,
                    read_message, relay_error_of)
from .relay import RELAY_COMMAND

HELLO_TIMEOUT_S = 30.0
# The relay waits up to the socket client's own deadline for the daemon; the SSH hop gets a margin on top.
DEFAULT_TIMEOUT_S = SOCKET_TIMEOUT_S + 15.0
CLOSE_GRACE_S = 5.0
STDERR_TAIL_BYTES = 4096
STOP_METHOD = "run.stop"

# Every option is given on the command line, which ssh reads before any config file, so these win; and no
# config file is read at all (-F none).
HARDENED_OPTIONS = (
    "BatchMode=yes",
    "StrictHostKeyChecking=yes",
    "GlobalKnownHostsFile=none",
    "UpdateHostKeys=no",
    "CheckHostIP=no",
    "VerifyHostKeyDNS=no",
    "ForwardAgent=no",
    "ForwardX11=no",
    "ClearAllForwardings=yes",
    "PermitLocalCommand=no",
    "RequestTTY=no",
    "ControlMaster=no",
    "ControlPath=none",
    "ConnectTimeout=15",
    "ServerAliveInterval=15",
    "ServerAliveCountMax=3",
    "LogLevel=ERROR",
)

_HOST = re.compile(r"[A-Za-z0-9_.:%-]{1,253}")
_USER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")
# Absolute, POSIX or Windows. No whitespace (ssh splits a UserKnownHostsFile value on it), no '%' or '~'
# (ssh expands them), no quotes. Every pattern is matched against the whole string.
_PATH = re.compile(r"(?:/|[A-Za-z]:[\\/])[A-Za-z0-9._/\\+-]{1,1023}")
_FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")


class RelayRefused(TransportRefused):
    """Base class for the SSH transport's own refusals."""


class HostKeyNotPinned(RelayRefused, ValueError):
    """The known_hosts file does not trust exactly the expected key for this host; ssh is not started."""


class RelayNotDelivered(RelayRefused, ConnectionError):
    """The request did not reach the daemon. ``detail`` says where it stopped."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class RelayHandshakeFailed(RelayNotDelivered):
    """The SSH session never came up: ssh failed, or the relay did not answer its hello."""


class RelayDeliveryUnknown(RelayRefused, ConnectionError):
    """The request was written whole and no complete answer came back; it may have been carried out."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class SshTarget:
    """Where the relay is and how its host key is pinned. Every field is checked; nothing has a hidden default.

    ``known_hosts`` is a file of the operator's own, kept for this host, that must trust exactly one key
    for it: ``host_key_fingerprint`` (``SHA256:...``, as ``ssh-keygen -lf`` prints it on the host). No ssh
    configuration file is read (``-F none``); a named configuration is not supported in this slice.
    """

    host: str
    known_hosts: str
    host_key_fingerprint: str
    user: str | None = None
    port: int | None = None
    identity_file: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or self.host.startswith("-") or not _HOST.fullmatch(self.host):
            raise ValueError("host must be a host name or address, with no option, user or space in it")
        if self.user is not None and (not isinstance(self.user, str) or not _USER.fullmatch(self.user)):
            raise ValueError("user must be a plain login name")
        if self.port is not None and (type(self.port) is not int or not 1 <= self.port <= 65535):
            raise ValueError("port must be an integer from 1 to 65535")
        for name in ("known_hosts", "identity_file"):
            value = getattr(self, name)
            if value is None and name == "identity_file":
                continue
            if not isinstance(value, str) or not _PATH.fullmatch(value):
                raise ValueError(f"{name} must be an absolute path without spaces, '%', '~' or quotes")
        if not isinstance(self.host_key_fingerprint, str) or not _FINGERPRINT.fullmatch(self.host_key_fingerprint):
            raise ValueError("host_key_fingerprint must be SHA256:<43 base64 characters>")


def ssh_argv(target: SshTarget, *, ssh: Sequence[str] = ("ssh",)) -> list[str]:
    """The exact command line: no config file, hardened options, then ``--``, the host and the relay command."""
    argv = [*ssh, "-T", "-e", "none", "-F", "none", "-o", f"UserKnownHostsFile={target.known_hosts}"]
    for option in HARDENED_OPTIONS:
        argv += ["-o", option]
    if target.identity_file is not None:
        argv += ["-o", "IdentitiesOnly=yes", "-i", target.identity_file]
    if target.port is not None:
        argv += ["-p", str(target.port)]
    if target.user is not None:
        argv += ["-l", target.user]
    return [*argv, "--", target.host, RELAY_COMMAND]


def key_fingerprint(key_type: str, blob_b64: str) -> str | None:
    """``SHA256:<base64, unpadded>`` of a public key blob, as ssh prints it; ``None`` for a malformed key."""
    try:
        blob = base64.b64decode(blob_b64, validate=True)
    except ValueError:
        return None
    if len(blob) < 4:
        return None
    (length,) = struct.unpack(">I", blob[:4])
    if blob[4:4 + length] != key_type.encode("ascii", "replace"):
        return None
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


def known_host_name(host: str, port: int | None) -> str:
    """The name ssh looks up: the host in lower case, or ``[host]:port`` for a port other than 22."""
    name = host.lower()
    return name if port in (None, 22) else f"[{name}]:{port}"


def _glob(pattern: str, name: str) -> bool:
    """ssh's host pattern: ``*`` and ``?`` only; every other character, brackets too, is literal."""
    regex = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern.lower())
    return re.fullmatch(regex, name) is not None


def _pattern_matches(pattern: str, name: str) -> bool:
    if pattern.startswith("|1|"):
        try:
            salt_b64, digest_b64 = pattern[3:].split("|")
            salt = base64.b64decode(salt_b64, validate=True)
            digest = base64.b64decode(digest_b64, validate=True)
        except ValueError:
            return False
        return hmac.compare_digest(hmac.new(salt, name.encode("utf-8"), hashlib.sha1).digest(), digest)
    return _glob(pattern, name)


def pinned_fingerprints(path: str, host: str, port: int | None = None) -> frozenset[str]:
    """The fingerprints ssh would accept for ``host`` from the known_hosts file at ``path`` (and no other).

    Plain, wildcard and hashed (``|1|``) host patterns and ``!`` negation are read as ssh reads them. A key
    on any ``@revoked`` line is revoked for every host. A ``@cert-authority`` line that matches the host is
    refused (``HostKeyNotPinned``): ssh would accept any host certificate it signed, which no fingerprint
    here can name. A line with a marker ssh does not define is refused too.
    """
    name = known_host_name(host, port)
    pins: set[str] = set()
    revoked: set[str] = set()
    with open(path, encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            fields = raw.split()
            if not fields or fields[0].startswith("#"):
                continue
            marker = fields.pop(0) if fields[0].startswith("@") else None
            if marker not in (None, "@revoked", "@cert-authority"):
                raise HostKeyNotPinned(f"{path} holds a line with the unknown marker {marker!r}")
            if len(fields) < 3:
                continue
            patterns, key_type, blob = fields[0], fields[1], fields[2]
            fingerprint = key_fingerprint(key_type, blob)
            if marker == "@revoked":
                if fingerprint is not None:
                    revoked.add(fingerprint)
                continue
            matched = negated = False
            for pattern in patterns.split(","):
                if pattern.startswith("!"):
                    negated = negated or _pattern_matches(pattern[1:], name)
                else:
                    matched = matched or _pattern_matches(pattern, name)
            if not matched or negated:
                continue
            if marker == "@cert-authority":
                raise HostKeyNotPinned(f"{path} trusts a certificate authority for {name}; pin one host key instead")
            if fingerprint is not None:
                pins.add(fingerprint)
    return frozenset(pins - revoked)


def _kill(proc: Any) -> None:
    try:
        proc.kill()
    except OSError:
        pass


def _positive(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        raise ValueError(f"{name} must be a positive number")
    return value


class SshRelayClient:
    """One SSH session to a relay: ``request(message) -> reply`` and ``close()`` (a ``TransportClient``).

    The session is up once the relay has answered its hello; any failure before that is
    ``RelayHandshakeFailed`` (not delivered). A failure of the stream after a request was written whole
    closes the session for good, and is ``RelayDeliveryUnknown``. When the caller's message carries an
    ``id``, the answer must carry the same one, except for the relay's own refusal of a frame it could not
    read or copy, which carries none. ``spawn`` (``subprocess.Popen``) and ``ssh`` (the argv prefix) are
    injectable, so tests run a fake ssh and never a real one.
    """

    def __init__(
        self,
        target: SshTarget,
        *,
        ssh: Sequence[str] = ("ssh",),
        spawn: Callable[..., Any] = subprocess.Popen,
        env: dict[str, str] | None = None,
        hello_timeout_s: float = HELLO_TIMEOUT_S,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_frame_bytes: int = MAX_FRAME_BYTES,
    ) -> None:
        if not isinstance(target, SshTarget):
            raise TypeError("target must be an SshTarget")
        if type(max_frame_bytes) is not int or max_frame_bytes < 1:
            raise ValueError("max_frame_bytes must be a positive integer")
        hello_timeout_s = _positive(hello_timeout_s, "hello_timeout_s")
        self._timeout_s = _positive(timeout_s, "timeout_s")
        pins = pinned_fingerprints(target.known_hosts, target.host, target.port)
        if pins != {target.host_key_fingerprint}:
            raise HostKeyNotPinned(
                f"{target.known_hosts} must accept exactly {target.host_key_fingerprint} for this host; "
                f"it accepts {len(pins)} key(s)" + ("" if target.host_key_fingerprint in pins else ", not that one")
            )
        self.target = target
        self.label: dict[str, Any] | None = None
        self._max_frame_bytes = max_frame_bytes
        self._lock = threading.Lock()
        # At most one answer and the end of the stream are ever waiting; more means the far side is out of step.
        self._replies: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=2)
        self._out_of_step = threading.Event()
        self._stderr_tail = bytearray()
        self._stderr_lock = threading.Lock()
        self.argv = ssh_argv(target, ssh=ssh)
        self._proc = spawn(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           bufsize=0, env=env)
        self._reader = threading.Thread(target=self._read_replies, args=(self._proc.stdout,),
                                        name="remote-ssh-replies", daemon=True)
        self._drain = threading.Thread(target=self._drain_stderr, args=(self._proc.stderr,),
                                       name="remote-ssh-stderr", daemon=True)
        self._reader.start()
        self._drain.start()
        try:
            hello = self._handshake(hello_timeout_s)
        except BaseException:
            self._close_locked()
            raise
        self.label = {
            "transport": "ssh",
            "host": target.host,
            "port": target.port or 22,
            "host_key_fingerprint": target.host_key_fingerprint,
            "relay": hello,
        }

    @property
    def closed(self) -> bool:
        return self._proc is None

    def last_stderr(self) -> str:
        """What ssh (or the relay) last wrote on stderr, printable ASCII only: for the operator, never parsed."""
        with self._stderr_lock:
            text = bytes(self._stderr_tail).decode("ascii", "replace")
        return "".join(c if c == "\n" or (c.isascii() and c.isprintable()) else "?" for c in text)

    def _handshake(self, timeout_s: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        sent = self._send(encode_frame({"op": HELLO_OP, "relay_version": RELAY_VERSION}), deadline)
        if sent in ("write_timeout", "write_unknown"):
            raise RelayHandshakeFailed("hello_timeout")  # no request was sent: the session never came up
        if sent is not True:
            raise RelayHandshakeFailed(self._ended(sent))
        try:
            kind, value = self._replies.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            raise RelayHandshakeFailed("hello_timeout") from None
        if kind != "frame":
            raise RelayHandshakeFailed(self._ended(f"stream:{value}") if kind == "eof" else f"stream:{value}")
        relay = value.get("relay")
        if value.get("ok") is not True or value.get("op") != HELLO_OP or not isinstance(relay, dict):
            raise RelayHandshakeFailed("relay_hello_invalid")
        version = relay.get("version")
        if type(version) is not int or version != RELAY_VERSION:
            raise RelayHandshakeFailed("relay_version")
        return dict(relay)

    def _ended(self, fallback: str) -> str:
        """After the stream ended: ``ssh_exit_<code>`` when ssh exited with an error, else ``fallback``."""
        code = self._close_locked()
        return f"ssh_exit_{code}" if isinstance(code, int) and code != 0 else fallback

    def request(self, message: Message) -> Message:
        """Send one frame and return its answer; the socket's busy and not-admitted frames raise as locally."""
        with self._lock:
            if self._proc is None:
                raise RelayNotDelivered("session_closed")
            if self._out_of_step.is_set() or not self._replies.empty():
                # Something arrived with no request outstanding: the session ended (or is out of step).
                self._close_locked()
                raise RelayNotDelivered("session_ended")
            refusal = frame_refusal(message, max_bytes=self._max_frame_bytes)
            if refusal is not None:
                raise RelayNotDelivered(f"request:{refusal}")  # nothing written; the session is intact
            deadline = time.monotonic() + self._timeout_s
            sent = self._send(encode_frame(message, max_bytes=self._max_frame_bytes), deadline)
            if sent == "write_unknown":
                # The writer did not show that bytes stayed unsent: the whole frame may have reached the relay.
                self._close_locked()
                raise RelayDeliveryUnknown("request:write_unknown")
            if sent is not True:
                # The write failed with bytes unsent (ssh stopped reading, or the deadline's kill ended it):
                # the relay acts only on whole frames, so this request was not carried out.
                self._close_locked()
                raise RelayNotDelivered(f"request:{sent}")
            try:
                kind, value = self._replies.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                self._close_locked()
                raise RelayDeliveryUnknown("request:timeout") from None
            if kind != "frame":
                self._close_locked()
                raise RelayDeliveryUnknown(f"request:{value}")
            reply: Message = value
            if reply == BUSY_MESSAGE:
                raise TransportBusy("busy: the daemon's socket was at its connection cap; request not delivered")
            if reply == NOT_ADMITTED_MESSAGE:
                raise PeerNotAdmitted("scope_refused: the daemon did not admit the relay; request not delivered")
            error = relay_error_of(reply)
            if error is not None and "id" not in reply:
                return self._relay_refusal(error, reply)  # the relay's own; it could not copy an id
            if "id" in message and reply.get("id") != message["id"]:
                self._close_locked()
                raise RelayDeliveryUnknown("request:reply_id_mismatch")
            if error is not None:
                return self._relay_refusal(error, reply)
            return reply

    def _relay_refusal(self, error: dict[str, Any], reply: Message) -> Message:
        detail = f"relay:{error.get('detail') or error.get('code')}"
        if error.get("code") == "bad_frame":
            self._close_locked()  # the relay may end its session after a frame it could not read
        if error["delivered"] is False:
            raise RelayNotDelivered(detail)
        if error["delivered"] == "unknown":
            raise RelayDeliveryUnknown(detail)
        return reply  # delivered: the daemon had it; RelayAPI reads the rest

    def close(self) -> None:
        """End the session. A request blocked in it is ended too: its ssh child is killed first."""
        if not self._lock.acquire(blocking=False):
            proc = self._proc
            if proc is not None:
                _kill(proc)
            self._lock.acquire()
        try:
            self._close_locked()
        finally:
            self._lock.release()

    def __enter__(self) -> SshRelayClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- the child process ------------------------------------------------------------------------
    def _send(self, frame: bytes, deadline: float) -> bool | str:
        """Write ``frame`` whole by ``deadline``.

        ``True``: every byte was handed to ssh (a flush that fails afterwards does not undo that).
        ``write_timeout`` or ``write:<error>``: the write failed with bytes still unsent, so the frame never
        went out whole (the deadline's kill ends a blocked write with such a failure). ``write_unknown``: the
        writer showed neither, because it is still running after the kill and its grace or it ended on
        something other than an I/O failure, so the frame may have gone out whole. Only a shown failure counts
        as "not delivered".

        The write runs in a thread of its own, so that a far side that stops reading cannot hold the caller
        past the deadline; at the deadline the ssh child is killed, which ends the write. The writer publishes
        its outcome once, as one pair, when it ends; the caller reads that pair and nothing else, so it can
        never combine a part of the outcome from before the writer finished with a part from after.
        """
        proc = self._proc
        if proc is None:
            return "write:session_closed"
        final: list[tuple[bool, str | None]] = []
        done = threading.Event()

        def write() -> None:
            all_sent, error = False, None
            try:
                data = memoryview(frame)
                while data:
                    written = proc.stdin.write(data)
                    if written is None or written <= 0:
                        raise BrokenPipeError("ssh took no bytes")
                    data = data[written:]
                all_sent = True  # before the flush: from here the frame may have gone out whole
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                error = f"write:{type(exc).__name__}"
            finally:
                final.append((all_sent, error))  # one consistent pair, published once
                done.set()

        writer = threading.Thread(target=write, name="remote-ssh-writer", daemon=True)
        writer.start()
        timed_out = not done.wait(max(0.0, deadline - time.monotonic()))
        if timed_out:
            _kill(proc)
            done.wait(CLOSE_GRACE_S)
        writer.join(CLOSE_GRACE_S)
        if not final:
            return "write_unknown"  # still running after the kill and its grace: nothing shown either way
        all_sent, error = final[0]
        if all_sent:
            return True
        if error is not None:
            return "write_timeout" if timed_out else error
        return "write_unknown"  # it ended on something other than an I/O failure

    def _close_locked(self) -> int | None:
        """End the session: close ssh's stdin (the relay sees end of input), wait, kill if it lingers."""
        proc, self._proc = self._proc, None
        if proc is None:
            return None
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            code = proc.wait(timeout=CLOSE_GRACE_S)
        except subprocess.TimeoutExpired:
            _kill(proc)
            code = proc.wait()
        for thread in (self._reader, self._drain):
            if thread is not threading.current_thread():
                thread.join(timeout=CLOSE_GRACE_S)
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        return code

    def _put(self, item: tuple[str, Any]) -> bool:
        try:
            self._replies.put_nowait(item)
        except queue.Full:
            self._out_of_step.set()  # more frames than requests: stop reading; the next request ends the session
            return False
        return True

    def _read_replies(self, stdout: Any) -> None:
        try:
            while True:
                message = read_message(stdout, max_bytes=self._max_frame_bytes)
                if message is None:
                    self._put(("eof", "eof"))
                    return
                if not self._put(("frame", message)):
                    return
        except TransportRefused as exc:  # FrameError: a frame that could not be read
            self._put(("error", str(exc)))
        except (OSError, ValueError) as exc:
            self._put(("error", type(exc).__name__))

    def _drain_stderr(self, stderr: Any) -> None:
        try:
            while True:
                chunk = stderr.read(1024)
                if not chunk:
                    return
                with self._stderr_lock:
                    self._stderr_tail += chunk
                    del self._stderr_tail[:-STDERR_TAIL_BYTES]
        except (OSError, ValueError):
            return


def _connect_failure(err: BaseException) -> dict[str, Any]:
    detail = err.detail if isinstance(err, RelayNotDelivered) else type(err).__name__
    return {"code": UNAVAILABLE, "delivered": False, "detail": f"connect:{detail}"}


class RelayAPI:
    """``call(method, params) -> dict`` over the relay, answering as ``control.client.SocketAPI`` does.

    ``connect()`` makes one session (default: ``SshRelayClient(target, **client_options)``). Each call
    gets a fresh session, unless ``persistent`` is set; ``run.stop`` always gets a fresh one, and does not
    wait for the shared session.
    """

    def __init__(self, target: SshTarget | None = None, *, persistent: bool = False,
                 connect: Callable[[], Any] | None = None, **client_options: Any) -> None:
        if target is not None and not isinstance(target, SshTarget):
            raise TypeError("target must be an SshTarget")
        if connect is None:
            if target is None:
                raise ValueError("RelayAPI needs a target or a connect function")
            connect = lambda: SshRelayClient(target, **client_options)  # noqa: E731 - one-line factory
        elif not callable(connect):
            raise TypeError("connect must be a function that opens a session")
        self.target = target
        self.label: dict[str, Any] | None = None
        self._connect = connect
        self._persistent = persistent
        self._session: Any = None
        self._lock = threading.Lock()

    def call(self, method: str, params: dict | None = None) -> dict:
        if self._persistent and method != STOP_METHOD:
            with self._lock:
                return self._call_persistent(method, params)
        try:
            client = self._open()
        except (OSError, TransportRefused) as err:
            return _connect_failure(err)
        try:
            return self._ask(client, method, params)
        finally:
            client.close()

    def close(self) -> None:
        with self._lock:
            session, self._session = self._session, None
        if session is not None:
            session.close()

    def _call_persistent(self, method: str, params: dict | None) -> dict:
        for attempt in (1, 2):
            if self._session is None:
                try:
                    self._session = self._open()
                except (OSError, TransportRefused) as err:
                    return _connect_failure(err)
            answer = self._ask(self._session, method, params)
            if getattr(self._session, "closed", False):
                self._session = None
                # A session that had ended while idle took nothing: open a new one and ask once more.
                if attempt == 1 and answer.get("delivered") is False and answer.get("detail") == "session_ended":
                    continue
            return answer
        return answer  # pragma: no cover - the loop always returns

    def _open(self) -> Any:
        client = self._connect()
        self.label = getattr(client, "label", None)
        return client

    def _ask(self, client: Any, method: str, params: dict | None) -> dict:
        message = {"op": "control", "method": method, "params": dict(params or {})}  # SocketAPI's own frame
        try:
            reply = client.request(message)
        except TransportBusy:
            return {"code": UNAVAILABLE, "delivered": False, "detail": "busy"}
        except PeerNotAdmitted:
            return {"code": UNAVAILABLE, "delivered": False, "detail": "daemon:scope_refused"}
        except RelayNotDelivered as err:
            return {"code": UNAVAILABLE, "delivered": False, "detail": err.detail}
        except RelayDeliveryUnknown as err:
            return {"code": UNAVAILABLE, "delivered": "unknown", "detail": err.detail}
        except (OSError, TransportRefused) as err:
            return {"code": UNAVAILABLE, "delivered": "unknown", "detail": f"request:{type(err).__name__}"}
        error = relay_error_of(reply)
        if error is not None:
            # The daemon carried the request out and its answer did not come back whole.
            return {"code": UNAVAILABLE, "delivered": True, "detail": f"relay:{error.get('code')}"}
        result = reply.get("result") if reply.get("ok") is True else None
        if isinstance(result, dict) and isinstance(result.get("code"), str):
            return result
        error = reply.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        return {"code": UNAVAILABLE, "delivered": True, "detail": f"daemon:{code if isinstance(code, str) else 'invalid_reply'}"}
