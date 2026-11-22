"""Run one plugin on the host end of a daemon channel."""

from __future__ import annotations

import base64
import io
import os
import sys
import types
from collections import deque
from contextlib import contextmanager
from itertools import count
from threading import get_ident

from recursion_core.control_transport.errors import FrameError
from recursion_core.control_transport.framing import HEADER
from recursion_core.plugin_security import host_channel as hc
from recursion_core.plugin_security.limits import ARTIFACT
from recursion_core.plugin_security.runtime import GilProbe

# This becomes a setting in the person's own settings file. The daemon alone passes it.
DEFAULT_GIL_CHECK = "on"


def _close_inherited():
    first = 2 + 1
    if os.name == "nt":
        # The Windows UCRT low-level descriptor table ends here.
        os.closerange(first, 8192)
        return
    if sys.platform.startswith("linux"):
        try:
            descriptors = os.listdir("/proc/self/fd")
        except OSError:
            pass
        else:
            # The directory iterator's fd has already closed. An fd can also
            # be above the current limit if the parent lowered that limit.
            for entry in descriptors:
                fd = int(entry)
                if fd >= first:
                    os.closerange(fd, fd + 1)
            return

    import resource

    _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    limit = os.sysconf("SC_OPEN_MAX") if hard == resource.RLIM_INFINITY else hard
    os.closerange(first, limit)


class _Channel:
    """Own the two streams and carry the channel codec over them."""

    def __init__(self, reader, writer):
        self._reader, self._writer = reader, writer

    @classmethod
    @contextmanager
    def from_stdio(cls):
        with os.fdopen(os.dup(0), "rb") as reader, os.fdopen(os.dup(1), "wb") as writer:
            for stream in (reader, writer):
                os.set_inheritable(stream.fileno(), False)
            with open(os.devnull, "wb") as null:
                os.dup2(null.fileno(), 1)
            sys.stdout = sys.__stdout__ = open(1, "w", encoding="utf-8", buffering=1, closefd=False)
            yield cls(reader, writer)

    def receive(self):
        header = self._reader.read(HEADER.size)
        if not header:
            return None
        if len(header) != HEADER.size:
            raise hc.ChannelRefusal("message_invalid", "frame_truncated")
        (length,) = HEADER.unpack(header)
        if length > hc.frame_limit("daemon"):
            # Consume this frame so the next header is still at a frame boundary.
            while length:
                chunk = self._reader.read(min(length, io.DEFAULT_BUFFER_SIZE))
                if not chunk:
                    break
                length -= len(chunk)
            raise hc.ChannelRefusal("message_invalid", "frame_too_large")
        body = self._reader.read(length)
        if len(body) != length:
            raise hc.ChannelRefusal("message_invalid", "frame_truncated")
        return hc.decode(body, sender="daemon")

    def send(self, message):
        frame = hc.encode(hc.check_message(message, receiver="daemon"))
        self._writer.write(frame)
        self._writer.flush()


class _Package:
    def __init__(self, files):
        self._files = files

    def read(self, path):
        try:
            return self._files[path]
        except KeyError:
            raise FileNotFoundError(path) from None


def _check_json(value):
    """Check the return value before the encoder can coerce tuples or keys."""
    kind = type(value)
    if value is None or any(kind is scalar for scalar in (str, bool, int, float)):
        return
    if kind is list:
        for item in value:
            _check_json(item)
        return
    if kind is dict:
        for key, item in value.items():
            if type(key) is not str:
                break
            _check_json(item)
        else:
            return
    raise TypeError("expected a JSON value")


class _Program:
    def __init__(self, channel, probe):
        self._channel, self._probe = channel, probe
        self._handlers = {}
        self._end = hc.HostEnd(self._handlers)
        self._deferred = deque()
        self._core_ids = count()
        self._thread = get_ident()
        self._plugin_id = self._module = None
        self._greeted = self._closed = self._gil_latched = False
        self._gil_check, self._gil = DEFAULT_GIL_CHECK, "unknown"

    def status(self) -> dict:
        """Snapshot of the GIL gate and the latest GIL reading."""
        return {"gil_check": self._gil_check, "gil": self._gil, "accepting_work": not self._gil_latched}

    def serve(self):
        self._channel.send({"type": "hello", "channel": hc.CHANNEL})
        while not self._closed:
            message = self._deferred.popleft() if self._deferred else self._read_peer()
            if message is not None:
                self._dispatch(message)

    def _read_peer(self):
        while True:
            message = None
            try:
                message = self._channel.receive()
                if message is None:
                    self._closed = True
                    return None
                hc.check_message(message, receiver="host")
                kind = message["type"]
                if kind == "bye":
                    self._closed = True
                    return None
                if kind == "hello":
                    if self._greeted:
                        raise hc.ChannelRefusal("message_invalid", "hello: already received")
                    self._greeted = True
                    self._gil_check = message.get("gil_check", DEFAULT_GIL_CHECK)
                    continue
                if not self._greeted and kind != "refusal":
                    raise hc.ChannelRefusal("message_invalid", "expected hello")
                return message
            except hc.ChannelRefusal as exc:
                self._channel.send(hc.refusal(message, exc))

    def _dispatch(self, message):
        kind = message["type"]
        if kind in ("load", "call"):
            self._work(message)
            return
        if kind == "core":
            answer = hc.refusal(message, hc.ChannelRefusal("message_invalid", "core: no pending request"))
        else:
            answer = self._end.receive(message)
        if answer is not None:
            self._channel.send(answer)

    def _work(self, message):
        if self._gil_latched:
            self._channel.send(self._gil_refusal(message))
            return
        answer = failure = None
        try:
            answer = self._load(message) if message["type"] == "load" else self._end.receive(message)
        except BaseException as exc:
            # A plugin can raise SystemExit too. Read the GIL before answering.
            failure = exc
        self._gil = "enabled" if self._probe.gil_enabled() else "disabled"
        if self._gil == "enabled" and self._gil_check != "off":
            self._gil_latched = True
            self._handlers.clear()
            self._module = None
            answer = self._gil_refusal(message)
        elif failure is not None:
            answer = self._failed(message, failure)
        if not self._closed and answer is not None:
            try:
                if answer["type"] == "result":
                    _check_json(answer["value"])
                self._channel.send(answer)
            except (FrameError, RecursionError, TypeError, ValueError) as exc:
                self._channel.send(self._failed(message, exc))

    @staticmethod
    def _gil_refusal(message):
        return hc.refusal(message, hc.ChannelRefusal("host_gil_enabled"))

    @staticmethod
    def _failed(message, exc):
        detail = type(exc).__name__.encode("utf-8", "backslashreplace").decode("utf-8")
        return hc.refusal(message, hc.ChannelRefusal("call_failed", detail))

    def _load(self, message):
        plugin_id = message["plugin_id"]
        if self._plugin_id is None:
            self._plugin_id = plugin_id
        if plugin_id != self._plugin_id or self._module is not None or ARTIFACT not in message["files"]:
            exc = hc.ChannelRefusal("message_invalid", "load: expected the channel's inactive plugin and its artifact")
            return hc.refusal(message, exc)

        # Verification and admission already ran. The manifest stays opaque.
        files = {path: base64.b64decode(data) for path, data in message["files"].items()}
        code = compile(files[ARTIFACT], "<plugin %s>" % plugin_id, "exec", dont_inherit=True)
        module = types.ModuleType("csl_plugin_" + plugin_id.replace(".", "_").replace("-", "_"))
        module.__dict__["package"] = _Package(files)
        exec(code, module.__dict__)
        bind = module.__dict__.get("bind_core")
        if callable(bind):
            bind(self._relay_core)
        self._handlers.update((name, module.__dict__.get(name)) for name in hc.CALL_NAMES)
        self._module = module
        return None

    def _relay_core(self, request: bytes) -> bytes:
        if get_ident() != self._thread:
            raise RuntimeError("core requests run on the plugin dispatch thread")
        if self._closed:
            raise EOFError("plugin channel ended")
        if type(request) is not bytes:
            raise TypeError("core requests are bytes")
        id_ = next(self._core_ids)
        self._channel.send({"type": "core", "id": id_, "bytes_b64": base64.b64encode(request).decode("ascii")})
        while (message := self._read_peer()) is not None:
            kind = message["type"]
            if kind == "core" and message["id"] == id_:
                return base64.b64decode(message["bytes_b64"])
            if kind == "refusal" and message["id"] == id_:
                raise hc.ChannelRefusal(message["code"], message["detail"])
            if kind in ("call", "load"):
                self._deferred.append(message)
            else:
                self._dispatch(message)
        raise EOFError("plugin channel ended")


def main(*, gil_probe: GilProbe | None = None) -> dict:
    """Start the stdio host and return its final status to the local caller."""
    _close_inherited()
    keep = {"SYSTEMROOT": os.environ["SYSTEMROOT"]} if os.name == "nt" and "SYSTEMROOT" in os.environ else {}
    os.environ.clear()
    os.environ.update(keep)
    with _Channel.from_stdio() as channel:
        program = _Program(channel, GilProbe() if gil_probe is None else gil_probe)
        program.serve()
    return program.status()
