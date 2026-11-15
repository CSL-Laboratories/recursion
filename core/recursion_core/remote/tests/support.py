"""Shared pieces for the remote tests: a stand-in daemon socket, made-up host keys, the fake ssh, a clock.

Nothing here connects to a real host or reads a real key, agent socket or known_hosts file. The host keys
are made up from a seed; the known_hosts files are written into each test's own scratch directory.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import shutil
import struct
import sys
import tempfile
from pathlib import Path

from recursion_core.control_transport import UnixControlServer, with_ping_health

# Bounds a hang so that a broken test fails instead of waiting for ever; never a claim about speed.
WATCHDOG_S = 60.0
FAKE_SSH = Path(__file__).resolve().with_name("fake_ssh.py")


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def scratch_dir(test, prefix: str = "rmt-") -> str:
    """A fresh directory under TMPDIR (outside every home folder), removed after the test."""
    path = tempfile.mkdtemp(prefix=prefix)
    test.addCleanup(shutil.rmtree, path, ignore_errors=True)
    return path


class SocketDaemon:
    """A real owner-only control socket under a scratch runtime directory, standing in for the daemon.

    It records every request its handler sees (``seen``) and answers with ``handler``, by default an
    ``ok`` that echoes the method and params. ``environ`` points ``XDG_RUNTIME_DIR`` at it, so the relay
    finds it exactly where it finds the daemon's socket.
    """

    def __init__(self, test, *, handler=None, **server_options) -> None:
        self.runtime = scratch_dir(test)
        self.directory = os.path.join(self.runtime, "recursion")
        self.environ = {"XDG_RUNTIME_DIR": self.runtime}
        self.seen: list[dict] = []
        self._inner = with_ping_health(handler or self.answer)
        self.server = UnixControlServer(self.directory, self._record, **server_options)
        self.server.start()
        test.addCleanup(self.server.close)
        self.socket_path = self.server.path

    def _record(self, message, conn):
        self.seen.append(message)
        return self._inner(message, conn)

    @staticmethod
    def answer(message, conn):
        return {"ok": True, "result": {"code": "ok", "method": message.get("method"), "params": message.get("params")}}


def _ssh_string(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def made_up_key(seed: str, key_type: str = "ssh-ed25519") -> tuple[str, str, str]:
    """A made-up public key: ``(key_type, base64 blob, SHA256 fingerprint)``. No real key is read."""
    blob = _ssh_string(key_type.encode("ascii")) + _ssh_string(hashlib.sha256(seed.encode("utf-8")).digest())
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    return key_type, base64.b64encode(blob).decode("ascii"), fingerprint


def hashed_host(name: str, salt: bytes) -> str:
    """A known_hosts ``|1|salt|hash`` entry for ``name``, as ``ssh-keygen -H`` writes it."""
    digest = hmac.new(salt, name.encode("utf-8"), hashlib.sha1).digest()
    return "|1|" + base64.b64encode(salt).decode("ascii") + "|" + base64.b64encode(digest).decode("ascii")


def write_known_hosts(directory: str, lines: list[str]) -> str:
    path = os.path.join(directory, "pinned_known_hosts")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("".join(line + "\n" for line in lines))
    return path


def fake_ssh_prefix() -> tuple[str, ...]:
    """The argv prefix that runs the fake ssh with this interpreter (the GIL kept off when it is off here)."""
    flags = ["-B"]
    is_gil_enabled = getattr(sys, "_is_gil_enabled", None)
    if is_gil_enabled is not None and not is_gil_enabled():
        flags += ["-X", "gil=0"]
    return (sys.executable, *flags, str(FAKE_SSH))


def fake_ssh_env(runtime: str, *, mode: str = "relay", log: str | None = None, key_label: str | None = "laptop") -> dict:
    """The fake ssh's whole environment: no HOME, no agent socket, nothing of the real user's."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "XDG_RUNTIME_DIR": runtime,
        "TMPDIR": tempfile.gettempdir(),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "FAKE_SSH_MODE": mode,
    }
    if log is not None:
        env["FAKE_SSH_LOG"] = log
    if key_label is not None:
        env["FAKE_SSH_KEY_LABEL"] = key_label
    return env
