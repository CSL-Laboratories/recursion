"""The SSH client transport (lane REMOTE): drive a daemon on another machine through its relay.

``RelayAPI(target).call(method, params)`` has the shape of ``ControlAPI.call`` and gives the answers the
CLI's socket client gives (``control.client.SocketAPI``), so the CLI, the TUI and the desktop app can use
either one unchanged. Underneath, ``SshRelayClient`` runs ``ssh ... -- <host> recursiond relay`` and
speaks the relay's frames (``codec``) on the child's stdin and stdout.

Delivery is reported as the socket client reports it, with one more hop to account for:

* ``delivered: false``: nothing reached the daemon. ssh could not start or log in, the host key is not
  the pinned one, the relay did not answer its hello, the daemon was not running behind the relay, or
  the socket was busy or did not admit the relay. Details begin ``connect:`` (before any request was
  written), ``relay:`` (the relay's own answer) or are the socket's own words (``busy``,
  ``daemon:scope_refused``).
* ``delivered: "unknown"``: the request was written and no complete answer came back (the SSH link or
  the relay died, or the answer did not arrive in time). It may have been carried out: read the run's
  status before retrying.
* Otherwise the daemon's answer, verbatim.

The host key is pinned, not trusted on first use: ``SshTarget.known_hosts`` names one known_hosts file
that must accept exactly one key for the host, and that key's fingerprint must be the one the caller
expects (``host_key_fingerprint``); ssh then runs with ``StrictHostKeyChecking=yes`` against that file
only. No config file is read unless the caller names one (``-F none``), and agent and X11 forwarding,
port forwarding, a TTY, connection sharing and local commands are all off. The relay's hello gives a
label (``SshRelayClient.label``) for the operator to see; it is evidence, not authority.

A Stop never waits behind another call: ``RelayAPI`` opens a fresh SSH session for each call, as the
socket client makes one connection per call. With ``persistent=True`` ordinary calls share one session
(for a surface that polls), and ``run.stop`` still gets a session of its own.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import itertools
import queue
import re
import struct
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from recursion_core.control.client import DEFAULT_TIMEOUT_S as SOCKET_TIMEOUT_S
from recursion_core.control_transport.errors import FrameError, PeerNotAdmitted, TransportBusy, TransportRefused
from recursion_core.control_transport.framing import MAX_FRAME_BYTES, encode_frame

from .codec import (BUSY_MESSAGE, HELLO_OP, NOT_ADMITTED_MESSAGE, RELAY_VERSION, UNAVAILABLE, Message, read_message,
                    relay_error_of)
from .relay import RELAY_COMMAND

HELLO_TIMEOUT_S = 30.0
# The relay waits up to the socket client's own deadline for the daemon; the SSH hop gets a margin on top.
DEFAULT_TIMEOUT_S = SOCKET_TIMEOUT_S + 15.0
CLOSE_GRACE_S = 5.0
STDERR_TAIL_BYTES = 4096
STOP_METHOD = "run.stop"

# Every option is given on the command line, which ssh reads before any config file, so these win.
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

_HOST = re.compile(r"^[A-Za-z0-9_.:%-]{1,253}$")
_USER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
# Absolute, POSIX or Windows. No whitespace (ssh splits a UserKnownHostsFile value on it), no '%' or '~'
# (ssh expands them), no quotes.
_PATH = re.compile(r"^(?:/|[A-Za-z]:[\\/])[A-Za-z0-9._/\\+-]{1,1023}$")
_FINGERPRINT = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")


class RelayRefused(TransportRefused):
    """Base class for the SSH transport's own refusals."""


class HostKeyNotPinned(RelayRefused, ValueError):
    """The known_hosts file does not accept exactly the expected key for this host; ssh is not started."""


class RelayNotDelivered(RelayRefused, ConnectionError):
    """The request did not reach the daemon. ``detail`` says where it stopped."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class RelayHandshakeFailed(RelayNotDelivered):
    """The SSH session never came up: ssh failed, or the relay did not answer its hello."""


class RelayDeliveryUnknown(RelayRefused, ConnectionError):
    """The request was written and no complete answer came back; it may have been carried out."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class SshTarget:
    """Where the relay is and how its host key is pinned. Every field is checked; nothing has a hidden default.

    ``known_hosts`` is a file of the operator's own, kept for this host, that must accept exactly one key
    for it: ``host_key_fingerprint`` (``SHA256:...``, as ``ssh-keygen -lf`` prints it on the host).
    ``ssh_config`` is ``"none"`` (read no config file) or an absolute path.
    """

    host: str
    known_hosts: str
    host_key_fingerprint: str
    user: str | None = None
    port: int | None = None
    identity_file: str | None = None
    ssh_config: str = "none"

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or self.host.startswith("-") or not _HOST.match(self.host):
            raise ValueError("host must be a host name or address, with no option, user or space in it")
        if self.user is not None and (not isinstance(self.user, str) or not _USER.match(self.user)):
            raise ValueError("user must be a plain login name")
        if self.port is not None and (type(self.port) is not int or not 1 <= self.port <= 65535):
            raise ValueError("port must be an integer from 1 to 65535")
        for name in ("known_hosts", "identity_file"):
            value = getattr(self, name)
            if value is None and name == "identity_file":
                continue
            if not isinstance(value, str) or not _PATH.match(value):
                raise ValueError(f"{name} must be an absolute path without spaces, '%', '~' or quotes")
        if self.ssh_config != "none" and (not isinstance(self.ssh_config, str) or not _PATH.match(self.ssh_config)):
            raise ValueError("ssh_config must be 'none' or an absolute path")
        if not isinstance(self.host_key_fingerprint, str) or not _FINGERPRINT.match(self.host_key_fingerprint):
            raise ValueError("host_key_fingerprint must be SHA256:<43 base64 characters>")


def ssh_argv(target: SshTarget, *, ssh: Sequence[str] = ("ssh",)) -> list[str]:
    """The exact command line: hardened options, then ``--``, the host and the relay command."""
    argv = [*ssh, "-T", "-e", "none", "-F", target.ssh_config, "-o", f"UserKnownHostsFile={target.known_hosts}"]
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

    Plain, wildcard and hashed (``|1|``) host patterns and ``!`` negation are read as ssh reads them. A
    key on any ``@revoked`` line is revoked for every host. ``@cert-authority`` lines pin no key here.
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
            if len(fields) < 3:
                continue
            patterns, key_type, blob = fields[0], fields[1], fields[2]
            fingerprint = key_fingerprint(key_type, blob)
            if fingerprint is None:
                continue
            if marker == "@revoked":
                revoked.add(fingerprint)
                continue
            if marker is not None:
                continue
            matched = negated = False
            for pattern in patterns.split(","):
                if pattern.startswith("!"):
                    negated = negated or _pattern_matches(pattern[1:], name)
                else:
                    matched = matched or _pattern_matches(pattern, name)
            if matched and not negated:
                pins.add(fingerprint)
    return frozenset(pins - revoked)


class SshRelayClient:
    """One SSH session to a relay: ``request(message) -> reply`` and ``close()`` (a ``TransportClient``).

    The session is up once the relay has answered its hello; any failure before that is
    ``RelayHandshakeFailed`` (not delivered). A failure of the stream after a request was written closes
    the session for good, and is ``RelayDeliveryUnknown``. ``spawn`` (``subprocess.Popen``) and ``ssh``
    (the argv prefix) are injectable, so tests run a fake ssh and never a real one.
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
        pins = pinned_fingerprints(target.known_hosts, target.host, target.port)
        if pins != {target.host_key_fingerprint}:
            raise HostKeyNotPinned(
                f"{target.known_hosts} must accept exactly {target.host_key_fingerprint} for this host; "
                f"it accepts {len(pins)} key(s)" + ("" if target.host_key_fingerprint in pins else ", not that one")
            )
        self.target = target
        self.label: dict[str, Any] | None = None
        self._timeout_s = timeout_s
        self._max_frame_bytes = max_frame_bytes
        self._lock = threading.Lock()
        self._replies: queue.Queue[tuple[str, Any]] = queue.Queue()
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
        return "".join(c if c.isprintable() or c == "\n" else "?" for c in text)

    def _handshake(self, timeout_s: float) -> dict[str, Any]:
        try:
            self._write(encode_frame({"op": HELLO_OP, "relay_version": RELAY_VERSION}))
        except OSError as exc:
            raise RelayHandshakeFailed(self._ended(f"write:{type(exc).__name__}")) from None
        try:
            kind, value = self._replies.get(timeout=timeout_s)
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
            if not self._replies.empty():
                # Something arrived with no request outstanding: the session ended (or is out of step).
                self._close_locked()
                raise RelayNotDelivered("session_ended")
            try:
                frame = encode_frame(message, max_bytes=self._max_frame_bytes)
            except FrameError as exc:
                raise RelayNotDelivered(f"request:{exc}") from None  # nothing written; the session is intact
            try:
                self._write(frame)
            except OSError as exc:
                # ssh has stopped reading: the relay runs only whole frames, so this one was not carried out.
                self._close_locked()
                raise RelayNotDelivered(f"request:write:{type(exc).__name__}") from None
            try:
                kind, value = self._replies.get(timeout=self._timeout_s)
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
            if "id" in message and reply.get("id") != message["id"]:
                self._close_locked()
                raise RelayDeliveryUnknown("request:reply_id_mismatch")
            error = relay_error_of(reply)
            if error is not None:
                detail = f"relay:{error.get('detail') or error.get('code')}"
                if error.get("code") == "bad_frame":
                    self._close_locked()  # the relay ends its session after a frame it could not read
                if error["delivered"] is False:
                    raise RelayNotDelivered(detail)
                if error["delivered"] == "unknown":
                    raise RelayDeliveryUnknown(detail)
            return reply

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def __enter__(self) -> SshRelayClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- the child process ------------------------------------------------------------------------
    def _write(self, frame: bytes) -> None:
        proc = self._proc
        if proc is None:
            raise BrokenPipeError("session closed")
        data = memoryview(frame)
        while data:
            written = proc.stdin.write(data)
            if written is None or written <= 0:
                raise BrokenPipeError("ssh took no bytes")
            data = data[written:]
        proc.stdin.flush()

    def _close_locked(self) -> int | None:
        """End the session: close ssh's stdin (the relay sees end of input), wait, kill if it lingers."""
        proc, self._proc = self._proc, None
        if proc is None:
            return None
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            code = proc.wait(timeout=CLOSE_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()
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

    def _read_replies(self, stdout: Any) -> None:
        try:
            while True:
                message = read_message(stdout, max_bytes=self._max_frame_bytes)
                if message is None:
                    self._replies.put(("eof", "eof"))
                    return
                self._replies.put(("frame", message))
        except FrameError as exc:
            self._replies.put(("error", str(exc)))
        except (OSError, ValueError) as exc:
            self._replies.put(("error", type(exc).__name__))

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
    gets a fresh session, unless ``persistent`` is set; ``run.stop`` always gets a fresh one.
    """

    def __init__(self, target: SshTarget | None = None, *, persistent: bool = False,
                 connect: Callable[[], Any] | None = None, **client_options: Any) -> None:
        if connect is None:
            if target is None:
                raise ValueError("RelayAPI needs a target or a connect function")
            connect = lambda: SshRelayClient(target, **client_options)  # noqa: E731 - one-line factory
        self.target = target
        self.label: dict[str, Any] | None = None
        self._connect = connect
        self._persistent = persistent
        self._session: Any = None
        self._lock = threading.Lock()
        self._ids = itertools.count(1)

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
        message = {"op": "control", "method": method, "params": dict(params or {}), "id": next(self._ids)}
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
