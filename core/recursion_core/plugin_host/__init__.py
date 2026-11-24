"""Run one plugin on the host end of a daemon channel.

What this host is and is not. Plugin code runs in this very process. Every frame on the channel is the
plugin's voice: the plugin can read the daemon's next message and write any frame this host may write,
by the channel's two descriptors, and neither a guard in this module nor a wall around the process can
keep it from those descriptors. The guards here (one plugin and one load per host, nothing before hello,
core requests only from the plugin's own thread) keep an honest plugin in order; they are not a
boundary. The boundary is on the daemon's side, which must check every request that arrives on the
channel against that plugin's grant and must take no result on trust (the daemon's reader of this
channel is a later draft and that rule is its first requirement), and the wall around the process,
which limits what the process can do to the machine.

The wall. Before it reads its first load, the host walls itself in (``wall_linux.apply_wall``, with the
Python install as the only path it may read) and proves that wall from inside (``wall_linux.probe_wall``,
with a canary file it writes in its own working folder just before the wall and its starter as the other
process); its hello carries the record (``wall.v1``). The canary is gone again before any plugin byte runs.
A host whose record is not proven, or whose wall was refused, answers every load with
``plugin_wall_unproven`` naming what is missing, loads nothing and runs no plugin byte. Nothing on
the channel, in a manifest, in a file or in a setting reaches that step: it runs once, before the host
reads any message, and nothing here offers a way around it. Outside Linux there is no wall, so no load
is ever taken there. Until the daemon's reader of this channel exists, do not run a plugin you do not
trust.
"""

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

from . import wall_linux

# This becomes a setting in the person's own settings file. The daemon alone passes it.
DEFAULT_GIL_CHECK = "on"


# The restart (POSIX). The host goes on only when the environment block it holds is clean, and it
# never decides by this marker alone: a starter can set any variable, the marker too. The block is clean
# when it holds nothing at all, or exactly this one variable with the value 1 and nothing else. Why this
# one: it is the only variable the restart itself passes on, and it is what the restarted image holds.
# On Linux the block is read from the kernel's own record (/proc/self/environ), to which the interpreter
# adds nothing: its locale step (PEP 538) sets LC_CTYPE only inside the running process, never in that
# record. Where that record cannot be read (no /proc), os.environ is all there is to read, and it can
# hide a starter's own LC_CTYPE, which the locale step may overwrite. So there an image counts as clean
# only when this host made it itself (it carries RESTARTED) and os.environ then holds the marker alone;
# the one name the interpreter itself may add to os.environ is LC_CTYPE, and it is set aside only with
# a value that locale step writes.
RESTART_GUARD = "CSL_PLUGIN_HOST_CLEAN"
_LOCALE_NAME, _LOCALE_VALUES = "LC_CTYPE", ("C.UTF-8", "C.utf8", "UTF-8")

# How many restarts there are is decided by the mechanism, not by a figure: a restart appends this one
# word to the command line, and an image that carries it never restarts again. A host whose block is
# still not clean after its one restart ends in one typed refusal. A starter that writes the word itself
# can only get that refusal, never a host that runs plugin code with the starter's variables.
RESTARTED = "--restarted-once"

# A host that cannot start clean says so on the channel (START_REFUSED, with the detail named below) and
# ends with one of these statuses. They are named parts of this program's interface, not settings:
# 71 (sysexits EX_OSERR) the restart could not be made; 78 (EX_CONFIG) the environment was not clean
# after the restart. 0 is a clean end and 1 an internal failure of the host.
START_REFUSED = "host_start_refused"
EXIT_RESTART_FAILED = 71
EXIT_ENVIRONMENT_NOT_CLEAN = 78

# A host whose own wall is not proven answers every load with this refusal and runs no plugin byte.
WALL_UNPROVEN = "plugin_wall_unproven"


def _install_paths():
    """The Python install this host runs from: the only paths its wall lets it read (the environment's
    own folder and the install it was made from; one folder when they are the same)."""
    return sorted({sys.prefix, sys.base_prefix})


# The probe's canary: a regular file this host writes in its own working folder just before the wall, named
# for this process. The probe must find it unreadable behind the wall, so a wall that leaves file reads open
# is never proven (a folder alone would not show that). It holds nothing of anyone's: one line of its own.
CANARY_PREFIX = ".csl-plugin-host-canary-"


def _canary():
    """(the canary's path, a call that removes it), or ('', None) when it cannot be made, which proves nothing.

    A walled host can remove no file, so a helper process, forked before the wall and holding only its end
    of a pipe, removes the canary when the host shuts that pipe after the probe (or when the host ends), and
    the host waits for it: a plugin finds its working folder as it was."""
    try:
        path = os.path.join(os.getcwd(), CANARY_PREFIX + str(os.getpid()))
        made = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError:
        return "", None
    try:
        try:
            os.write(made, b"canary\n")
        finally:
            os.close(made)
        read_end, write_end = os.pipe()
    except OSError:
        _remove_quietly(path)
        return "", None
    try:
        helper = os.fork()
    except OSError:
        os.close(read_end)
        os.close(write_end)
        _remove_quietly(path)
        return "", None
    if helper == 0:
        try:
            os.close(write_end)
            os.read(read_end, 1)  # the host shuts its end after the probe, or ends
            os.unlink(path)
        finally:
            os._exit(0)
    os.close(read_end)

    def remove():
        os.close(write_end)
        os.waitpid(helper, 0)
    return path, remove


def _remove_quietly(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _wall():
    """Wall this process in, then probe the wall from inside: (the wall.v1 record or None, the refusal).

    Runs once, before the host reads any message. The refusal is the code of ``apply_wall``'s refusal, or
    the name of what it raised, or '' when the wall applied. The probe runs either way: its checks are
    harmless where they are allowed, and a record that shows them allowed is not proven. Where the probe
    cannot run at all (not Linux), the record is None. The canary is made only where the wall can be (on
    Linux; elsewhere the wall refuses before any change) and is removed once the probe has run."""
    canary, remove = _canary() if sys.platform.startswith("linux") else ("", None)
    refused = ""
    try:
        try:
            wall_linux.apply_wall(_install_paths())
        except wall_linux.WallRefused as exc:
            refused = exc.code
        except Exception as exc:  # a path that cannot be opened, or any other failure: no wall is in place
            refused = "apply_wall_" + type(exc).__name__
        try:
            record = wall_linux.probe_wall(canary, os.getppid())
        except Exception:  # wall_system_unsupported, or a probe that cannot run: nothing is proven
            record = None
    finally:
        if remove is not None:
            try:
                remove()
            except OSError:
                pass
    return record, refused


def _wall_missing(record, refused):
    """What keeps the wall from being proven: the refusal, then each check of the record that the kernel
    did not refuse, then 'proven' when the record still does not say so. Empty only for a proven wall."""
    missing = [refused] if refused else []
    if type(record) is not dict:
        return missing + ["no_record"]
    missing += [name for name in wall_linux.RECORD_CHECKS if record.get(name) is not True]
    if record.get("proven") is not True:
        missing.append("proven")
    return missing


def _environment_is_clean():
    """True when the environment block holds nothing, or exactly RESTART_GUARD=1 (see above).

    Without the kernel's record it is true only for an image this host made by its own restart."""
    try:
        with open("/proc/self/environ", "rb") as record:
            held = [item for item in record.read().split(b"\0") if item]
    except OSError:
        held = None
    if held is not None:
        return held in ([], [RESTART_GUARD.encode() + b"=1"])
    if sys.orig_argv[-1:] != [RESTARTED]:
        return False
    environment = dict(os.environ)
    if environment.get(_LOCALE_NAME) in _LOCALE_VALUES:
        del environment[_LOCALE_NAME]
    return environment in ({}, {RESTART_GUARD: "1"})


def _refuse_start(detail, status):
    """Say on the starter's own standard output that this host cannot start clean, then end with status."""
    try:
        for message in ({"type": "hello", "channel": hc.CHANNEL},
                        hc.refusal(None, hc.ChannelRefusal(START_REFUSED, detail))):
            data = hc.encode(hc.check_message(message, receiver="daemon"))
            while data:
                data = data[os.write(1, data):]
    except OSError:
        pass  # the write side is not there; the exit status still says it
    raise SystemExit(status)


def _restart_with_clean_environment():
    """Go on only when the environment is clean; otherwise start this same program again, once.

    Clearing os.environ does not touch the block the kernel keeps for the process (it stays readable
    in /proc/self/environ), so the host replaces its own process image before anything else runs, with
    an environment of RESTART_GUARD=1 alone. The channel descriptors are unchanged by this: the new
    image reads the same standard descriptors. The new image's first word is the interpreter's own
    path, the one given to execve, so a host started by bare name through PATH keeps its prefix in an
    environment that has no PATH. An image that already carries RESTARTED and is still
    not clean is refused, and so is a restart that cannot be made. Windows has no such call (it would
    end the starter's handle on this process), so it is not done there and os.environ is cleared in
    main as before.
    """
    if os.name == "nt" or _environment_is_clean():
        return
    if sys.orig_argv[-1:] == [RESTARTED]:
        _refuse_start("environment_not_clean", EXIT_ENVIRONMENT_NOT_CLEAN)
    try:
        os.execve(sys.executable, [sys.executable, *sys.orig_argv[1:], RESTARTED], {RESTART_GUARD: "1"})
    except (OSError, ValueError, TypeError):
        _refuse_start("restart_failed", EXIT_RESTART_FAILED)


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
            # Plugin code gets the null device on all three standard descriptors, so a stray read
            # or print does not touch the channel or the starter. This is not a boundary: the channel's
            # two descriptors stay open in this process and plugin code can name them by number.
            with open(os.devnull, "rb") as null_in, open(os.devnull, "wb") as null_out:
                os.dup2(null_in.fileno(), 0)
                os.dup2(null_out.fileno(), 1)
                os.dup2(null_out.fileno(), 2)
            sys.stdin = sys.__stdin__ = open(0, "r", encoding="utf-8", closefd=False)
            sys.stdout = sys.__stdout__ = open(1, "w", encoding="utf-8", buffering=1, closefd=False)
            sys.stderr = sys.__stderr__ = open(
                2, "w", encoding="utf-8", errors="backslashreplace", buffering=1, closefd=False)
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
    def __init__(self, channel, probe, wall=(None, "wall_not_applied")):
        self._channel, self._probe = channel, probe
        self._wall_record, refused = wall
        self._wall_missing = _wall_missing(self._wall_record, refused)
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
        hello = {"type": "hello", "channel": hc.CHANNEL}
        if type(self._wall_record) is dict:
            hello["wall"] = self._wall_record
        self._channel.send(hello)
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
        if message["type"] == "load" and self._wall_missing:
            self._channel.send(self._wall_refusal(message))
            return
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

    def _wall_refusal(self, message):
        detail = ("the wall around this host is not proven, so it loads nothing and runs no plugin code; "
                  "missing: " + ", ".join(self._wall_missing))
        return hc.refusal(message, hc.ChannelRefusal(WALL_UNPROVEN, detail))

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
    _restart_with_clean_environment()
    _close_inherited()
    keep = {"SYSTEMROOT": os.environ["SYSTEMROOT"]} if os.name == "nt" and "SYSTEMROOT" in os.environ else {}
    os.environ.clear()
    os.environ.update(keep)
    with _Channel.from_stdio() as channel:
        # The wall comes after the inherited descriptors are shut and the standard ones point at the null
        # device, and before the first message is read or sent: nothing the daemon sends reaches it.
        wall = _wall()
        program = _Program(channel, GilProbe() if gil_probe is None else gil_probe, wall)
        program.serve()
    return program.status()
