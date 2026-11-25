"""Run one plugin on the host end of a daemon channel.

What this host is and is not. Plugin code runs in this very process. Every frame on the channel is the
plugin's voice: the plugin can read the daemon's next message and write any frame this host may write,
by the channel's two descriptors, and neither a guard in this module nor a wall around the process can
keep it from those descriptors. The guards here (one plugin and one load per host, nothing before hello,
core requests only from the plugin's own thread and one at a time) keep an honest plugin in order; they
are not a boundary. The boundary is on the daemon's side, which must check every request that arrives on the
channel against that plugin's grant and must take no result on trust (the daemon's reader of this
channel is a later draft and that rule is its first requirement), and the wall around the process,
which limits what the process can do to the machine. With this draft alone, do not run a plugin you
do not trust.
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
# still not clean after its one restart ends in one typed refusal. Where the kernel's record can be
# read, a starter that writes the word itself gets only that refusal, never a host that runs plugin code
# with the starter's variables. Where it cannot be read, such a starter whose os.environ holds the marker
# and at most one locale value of the kind the interpreter's locale step writes gets a running host: that
# one value is the starter's own, and main clears it from os.environ before any plugin code runs. No
# other variable gets past that check.
RESTARTED = "--restarted-once"

# A host that cannot start clean says so on the channel (START_REFUSED, with the detail named below) and
# ends with one of these statuses. They are named parts of this program's interface, not settings:
# 71 (sysexits EX_OSERR) the restart could not be made; 78 (EX_CONFIG) the environment was not clean
# after the restart. 0 is a clean end and 1 an internal failure of the host.
# 66 (EX_NOINPUT) the folder handed for Recursion's own code is not one that holds it, or Recursion's code
# there fails to import (see START_CODE).
START_REFUSED = "host_start_refused"
EXIT_RESTART_FAILED = 71
EXIT_ENVIRONMENT_NOT_CLEAN = 78
EXIT_FOLDER_REFUSED = 66

# What the daemon reads when a restart could not find this host's own code (the detail begins with the
# word restart_failed, as for every restart that could not be made).
RESTART_NOT_FOUND = (
    "restart_failed: the plugin host could not start again without the environment it was started with, "
    "because Recursion's own code was found only through that environment (PYTHONPATH), so no plugin code "
    "ran. Start the host with the folder that holds Recursion's code as its argument, as the daemon does, "
    "or reinstall Recursion by its recipe.")
FOLDER_REFUSED = "core_folder_not_usable"
_FOLDER_TEXT = (
    ": the plugin host could not start because the folder it was given for Recursion's own code is not "
    "an absolute folder that holds that code, so no plugin code ran. Reinstall Recursion by its recipe, or "
    "give the daemon's plugin hosts the folder that holds recursion_core. The folder given: ")
# What the daemon reads when the folder holds Recursion's code but that code fails to import: the exception's type and
# the first line of its words stand between the two parts.
CORE_CODE_FAILED = "core_code_failed"
_FAILED_TEXT = (
    ": the plugin host could not start because Recursion's own code in the folder it was given failed to import (")
_FAILED_TAIL = (
    "), so no plugin code ran. Reinstall Recursion by its recipe, or give the daemon's plugin hosts a folder whose "
    "recursion_core imports whole. The folder given: ")
# The channel's own limit on a refusal's detail (host_channel), not a setting.
_DETAIL_MAX = 1000

# How the daemon starts a host: python -I -c START_CODE <folder>. Isolated mode (-I) reads no variable of
# the environment, PYTHONPATH among them, and puts neither the working folder nor a script's folder on the
# import path; so the folder that holds Recursion's own code (the one with recursion_core in it) is handed
# as the one argument after this code and is never taken from the environment. This code puts that folder
# first on the import path, in the first image and in the image of the restart alike (the restart keeps
# the argv). A folder that is not absolute, is not a folder or holds no recursion_core is refused on the
# channel (START_REFUSED, detail FOLDER_REFUSED and the words above) and the host ends with
# EXIT_FOLDER_REFUSED; it writes those two frames itself, because the channel's codec is part of the code it
# could not find. A folder that holds that code but whose code fails to import (an import error inside the tree,
# a syntax error, any exception) is refused the same way and with the same status, with the detail
# CORE_CODE_FAILED and words that name the exception's type and the first line of its words; the words above stay
# for a folder where recursion_core or its plugin_host is not found. The code holds no dollar sign and no percent
# sign, so no launcher reads a variable or a specifier into it.
START_CODE = "\n".join((
    "import os, sys",
    "folder = sys.argv[1] if len(sys.argv) > 1 else ''",
    "start = None",
    "failed = ''",
    "if os.path.isabs(folder) and os.path.isdir(os.path.join(folder, 'recursion_core')):",
    "    sys.path.insert(0, folder)",
    "    try:",
    "        from recursion_core.plugin_host import start",
    "    except BaseException as exc:",
    "        start = None",
    "        if not (isinstance(exc, ModuleNotFoundError) and exc.name in %r):"
    % (('recursion_core', 'recursion_core.plugin_host'),),
    "            lines = str(exc).splitlines()",
    "            failed = type(exc).__name__ + ': ' + (lines[0] if lines else '')",
    "if start is None:",
    "    import json, struct",
    "    if failed:",
    "        detail = (%r + failed + %r + repr(folder))[:%d]" % (CORE_CODE_FAILED + _FAILED_TEXT, _FAILED_TAIL,
                                                               _DETAIL_MAX),
    "    else:",
    "        detail = (%r + repr(folder))[:%d]" % (FOLDER_REFUSED + _FOLDER_TEXT, _DETAIL_MAX),
    "    frames = b''",
    "    for message in ({'type': 'hello', 'channel': %r}," % hc.CHANNEL,
    "                    {'type': 'refusal', 'id': None, 'code': %r, 'detail': detail}):" % START_REFUSED,
    "        body = json.dumps(message).encode()",
    "        frames += struct.pack(%r, len(body)) + body" % HEADER.format,
    "    try:",
    "        while frames:",
    "            frames = frames[os.write(1, frames):]",
    "    except OSError:",
    "        pass",
    "    raise SystemExit(%d)" % EXIT_FOLDER_REFUSED,
    "start(folder)",
    "",
))
_handed_folder = None  # the folder START_CODE was given; None when the host was started another way


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
    """Say on the starter's own standard output that this host cannot start clean, then end with status.

    The hello and the refusal go in one write, so a daemon that reads the hello finds the refusal with it."""
    try:
        data = b"".join(hc.encode(hc.check_message(message, receiver="daemon"))
                        for message in ({"type": "hello", "channel": hc.CHANNEL},
                                        hc.refusal(None, hc.ChannelRefusal(START_REFUSED, detail))))
        while data:
            data = data[os.write(1, data):]
    except OSError:
        pass  # the write side is not there; the exit status still says it
    raise SystemExit(status)


def _found_after_restart():
    """True when the restarted image, which holds no variable but the marker, finds this very package.

    The restarted image has the same argv, so the same flags and the folder START_CODE puts first; what it
    loses is every variable, and the variable that adds folders to the import path is PYTHONPATH (isolated
    mode reads none). So its import path is this one without PYTHONPATH's folders, the handed folder kept,
    and each finder on sys.meta_path is asked with that path, as the restarted image would ask it."""
    taken = set()
    if not (sys.flags.isolated or sys.flags.ignore_environment):
        taken = {os.path.abspath(entry) for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep) if entry}
    if _handed_folder is not None:
        taken.discard(os.path.abspath(_handed_folder))
    path = [entry for entry in sys.path if os.path.abspath(entry) not in taken]
    own = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    saved = sys.path[:]
    sys.path[:] = path
    try:
        for finder in sys.meta_path:
            find = getattr(finder, "find_spec", None)
            spec = find(__name__.partition(".")[0], None) if find is not None else None
            if spec is not None:
                return own in {os.path.realpath(place) for place in spec.submodule_search_locations or ()}
    except Exception:  # noqa: BLE001 - a finder that cannot answer: the restart cannot be shown to find it
        return False
    finally:
        sys.path[:] = saved
    return False


def _restart_with_clean_environment():
    """Go on only when the environment is clean; otherwise start this same program again, once.

    Clearing os.environ does not touch the block the kernel keeps for the process (it stays readable
    in /proc/self/environ), so the host replaces its own process image before anything else runs, with
    an environment of RESTART_GUARD=1 alone. The channel descriptors are unchanged by this: the new
    image reads the same standard descriptors. The new image's first word is the interpreter's own
    path, the one given to execve, so a host started by bare name through PATH keeps its prefix in an
    environment that has no PATH. An image that already carries RESTARTED and is still
    not clean is refused, and so is a restart that cannot be made: one whose image would not find this
    host's own code (RESTART_NOT_FOUND), and one whose channel descriptors cannot be kept open across it.
    Windows has no such call (it would end the starter's handle on this process), so it is not done there
    and os.environ is cleared in main as before.
    """
    if os.name == "nt" or _environment_is_clean():
        return
    if sys.orig_argv[-1:] == [RESTARTED]:
        _refuse_start("environment_not_clean", EXIT_ENVIRONMENT_NOT_CLEAN)
    if not _found_after_restart():
        _refuse_start(RESTART_NOT_FOUND, EXIT_RESTART_FAILED)
    try:
        # The channel is descriptors 0 and 1. A starter may have left them close-on-exec, and execve would
        # then close them: the new image would have no channel and could say nothing.
        os.set_inheritable(0, True)
        os.set_inheritable(1, True)
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

    # A descriptor can sit above a limit the starter lowered, so the range ends past the largest of the
    # limits, the system's own figure and the highest descriptor the platform lists (/dev/fd on macOS and
    # the BSDs).
    bounds = [limit for limit in resource.getrlimit(resource.RLIMIT_NOFILE) if limit != resource.RLIM_INFINITY]
    try:
        bounds.append(os.sysconf("SC_OPEN_MAX"))
    except (OSError, ValueError):
        pass
    for listing in ("/dev/fd", "/proc/self/fd"):
        try:
            bounds.extend(int(entry) + 1 for entry in os.listdir(listing) if entry.isdigit())
        except OSError:
            pass
    os.closerange(first, max(bounds, default=first))


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


# The words of a core request made inside another one's wait on the same thread (a ChannelReentered).
_NESTED = ("core requests do not nest: an earlier core request of this plugin is still waiting for its answer; "
           "make this request after that one returns")


class _Program:
    def __init__(self, channel, probe):
        self._channel, self._probe = channel, probe
        self._handlers = {}
        self._end = hc.HostEnd(self._handlers)
        self._deferred = deque()
        self._core_ids = count()
        self._thread = get_ident()
        self._plugin_id = self._module = None
        self._greeted = self._closed = self._gil_latched = self._core_pending = False
        self._gil_check, self._gil = DEFAULT_GIL_CHECK, "unknown"

    def status(self) -> dict:
        """Snapshot of the GIL gate and the latest GIL reading."""
        return {"gil_check": self._gil_check, "gil": self._gil, "accepting_work": not self._gil_latched}

    def serve(self):
        """Send this host's hello, then answer the daemon until the channel ends.

        The channel ends at the end of input, or at the daemon's bye at ANY time, also before the
        daemon's hello and while a core request waits: the host then sends nothing more (its own
        hello is already out), reads no later frame and returns, so nothing after a bye is taken.
        """
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
        """Send one core request for the plugin and return the daemon's answer.

        ONE RULE for a request made from inside another on the same thread (plugin code that a trace or
        profile function, a signal handler or a finalizer runs): while a core request waits for its
        answer, or while this thread is inside the channel end's own section, a core request is refused at
        the plugin's own call with ``hc.ChannelReentered`` (a ``RuntimeError``) before anything is sent or
        read, so it never waits on the channel or on the end's lock and no answer comes late; the request
        the thread was busy with still gets its own answer, and a request made after it returns is sent
        as usual with the next id. A refusal of this request that arrives is raised as
        ``hc.ChannelRefusal`` whatever its code: a code this release does not list is kept as it came
        (``known`` is then False).
        """
        if get_ident() != self._thread:
            raise RuntimeError("core requests run on the plugin dispatch thread")
        if self._core_pending:
            raise hc.ChannelReentered(_NESTED)
        if self._end._inside():
            raise hc.ChannelReentered()
        if self._closed:
            raise EOFError("plugin channel ended")
        if type(request) is not bytes:
            raise TypeError("core requests are bytes")
        id_ = next(self._core_ids)
        self._core_pending = True
        try:
            self._channel.send({"type": "core", "id": id_, "bytes_b64": base64.b64encode(request).decode("ascii")})
            while (message := self._read_peer()) is not None:
                kind = message["type"]
                if kind == "core" and message["id"] == id_:
                    return base64.b64decode(message["bytes_b64"])
                if kind == "refusal" and message["id"] == id_:
                    raise hc._read_refusal(message)
                if kind in ("call", "load"):
                    self._deferred.append(message)
                else:
                    self._dispatch(message)
            raise EOFError("plugin channel ended")
        finally:
            self._core_pending = False


def start(folder):
    """The entry of START_CODE: run the host with Recursion's own code taken from ``folder``.

    ``folder`` is on the import path already. ``main`` is read here, at the call, so a stand-in for it
    is the one that runs."""
    global _handed_folder
    _handed_folder = folder
    return main()


def main(*, gil_probe: GilProbe | None = None) -> dict:
    """Start the stdio host and return its final status to the local caller."""
    _restart_with_clean_environment()
    _close_inherited()
    keep = {"SYSTEMROOT": os.environ["SYSTEMROOT"]} if os.name == "nt" and "SYSTEMROOT" in os.environ else {}
    os.environ.clear()
    os.environ.update(keep)
    with _Channel.from_stdio() as channel:
        program = _Program(channel, GilProbe() if gil_probe is None else gil_probe)
        program.serve()
    return program.status()
