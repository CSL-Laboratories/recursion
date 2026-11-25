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
process); its hello carries the record (``wall.v1``). The probe also tries each kind of write on that canary and in a
write folder made beside it for the purpose; both are gone again before any plugin byte runs, and a host that finds
either still there after its helper ended refuses every load.
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
import shutil
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
# 66 (EX_NOINPUT) the folder handed for Recursion's own code is not one that holds it (see START_CODE).
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
# could not find. The code holds no dollar sign and no percent sign, so no launcher reads a variable or a
# specifier into it.
START_CODE = "\n".join((
    "import os, sys",
    "folder = sys.argv[1] if len(sys.argv) > 1 else ''",
    "start = None",
    "if os.path.isabs(folder) and os.path.isdir(os.path.join(folder, 'recursion_core')):",
    "    sys.path.insert(0, folder)",
    "    try:",
    "        from recursion_core.plugin_host import start",
    "    except ImportError:",
    "        start = None",
    "if start is None:",
    "    import json, struct",
    "    detail = (%r + repr(folder))[:%d]" % (FOLDER_REFUSED + _FOLDER_TEXT, _DETAIL_MAX),
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

# A host whose own wall is not proven answers every load with this refusal and runs no plugin byte.
WALL_UNPROVEN = "plugin_wall_unproven"


def _install_paths():
    """The Python install this host runs from: the only paths its wall lets it read (the environment's
    own folder and the install it was made from; one folder when they are the same)."""
    return sorted({sys.prefix, sys.base_prefix})


# The probe's canary: a regular file this host writes in its own working folder just before the wall, named
# for this process. The probe must find it unreadable behind the wall, so a wall that leaves file reads open
# is never proven (a folder alone would not show that). It holds nothing of anyone's: one line of its own.
# Beside it, the write folder (the same name with WRITE_FOLDER_SUFFIX) is made for the probe's write kinds;
# the probe also tries the writes on the canary itself.
CANARY_PREFIX = ".csl-plugin-host-canary-"
WRITE_FOLDER_SUFFIX = ".d"


def _canary():
    """(the canary's path, the write folder's path, a call that removes both), or ('', '', None) when they
    cannot be made, which proves nothing.

    A walled host can remove no file, so a helper process, forked before the wall and holding only its end
    of a pipe, removes the canary and the write folder (with whatever a weak wall let the probe make in it)
    when the host shuts that pipe after the probe (or when the host ends), and the host waits for it. The
    call then answers the names still there (allowed behind the wall: it only looks), so a host whose helper
    was killed or could not remove them knows it: a plugin must find its working folder as it was."""
    try:
        path = os.path.join(os.getcwd(), CANARY_PREFIX + str(os.getpid()))
        made = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError:
        return "", "", None
    folder = path + WRITE_FOLDER_SUFFIX
    folder_made = False
    try:
        try:
            os.write(made, b"canary\n")
        finally:
            os.close(made)
        os.mkdir(folder, 0o700)
        folder_made = True
        wall_linux.prepare_write_folder(folder)
        read_end, write_end = os.pipe()
    except (OSError, ValueError):
        # A name already there by the write folder's name (a folder with what it holds, a file or a link) is not
        # this call's: it stays as it was, and only what this call made is removed.
        _remove_probe_files(path, folder if folder_made else None)
        return "", "", None
    try:
        helper = os.fork()
    except OSError:
        os.close(read_end)
        os.close(write_end)
        _remove_probe_files(path, folder)
        return "", "", None
    if helper == 0:
        try:
            os.close(write_end)
            os.read(read_end, 1)  # the host shuts its end after the probe, or ends
            _remove_probe_files(path, folder)
        finally:
            os._exit(0)
    os.close(read_end)

    def remove():
        os.close(write_end)
        os.waitpid(helper, 0)
        return [os.path.basename(name) for name in (path, folder) if os.path.lexists(name)]
    return path, folder, remove


def _remove_probe_files(path, folder):
    """The canary and the write folder with all it holds (no folder when ``folder`` is None); run by the helper,
    which the wall does not reach, and before the wall when a step of the setup fails."""
    _remove_quietly(path)
    if folder is not None:
        shutil.rmtree(folder, ignore_errors=True)


def _remove_quietly(path):
    try:
        os.unlink(path)
    except OSError:
        pass


# The fcntl command that reads a descriptor's owner for signals together with its kind (struct f_owner_ex: the
# kind, then the id, which is 0 when no owner is set). A number of the kernel's interface, not a setting. F_GETOWN
# is not used: it gives a process group as a negative number, which Python's fcntl takes for an error.
_F_GETOWN_EX = 16


def _signal_descriptors(unreadable=None):
    """The descriptors this process holds that would let the kernel send a signal for it, by number: those with
    an owner for signals of any kind (a process, a process group or a thread, read with F_GETOWN_EX) or with the
    asynchronous flag (O_ASYNC) set. Linux only, and read before the wall from /proc/self/fd, the kernel's own
    list of them, so a descriptor of any number is seen. A descriptor whose flags or owner cannot be read, and
    "unknown" when the descriptors cannot be listed, go into ``unreadable`` when a list is given, else among the
    others: either way the start is refused, never taken on trust. The owner is a property of the open file, so
    whoever shares it (the starter, the other end of a pipe) may have set it; a walled process that sets the
    asynchronous flag on such a descriptor makes the kernel signal that owner when the descriptor is ready."""
    import errno
    import fcntl
    import struct

    found, cannot = [], []
    try:
        listed = sorted(int(name) for name in os.listdir("/proc/self/fd"))
    except OSError:
        listed, cannot = [], ["unknown"]
    for descriptor in listed:
        try:
            if fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ASYNC:
                found.append(str(descriptor))
                continue
            _kind, owner = struct.unpack("ii", fcntl.fcntl(descriptor, _F_GETOWN_EX, bytes(8)))
        except OSError as exc:
            if exc.errno != errno.EBADF:  # EBADF: the listing's own descriptor, already shut
                cannot.append(str(descriptor))
            continue
        if owner != 0:
            found.append(str(descriptor))
    if unreadable is None:
        return found + cannot
    unreadable.extend(cannot)
    return found


def _wall():
    """Wall this process in, then probe the wall from inside: (the wall.v1 record or None, the refusal).

    Runs once, before the host reads any message. The refusal is the code of ``apply_wall``'s refusal, or
    the name of what it raised, or '' when the wall applied. The probe runs either way: its checks are
    harmless where they are allowed, and a record that shows them allowed is not proven. Where the probe
    cannot run at all (not Linux), the record is None. The canary is made only where the wall can be (on
    Linux; elsewhere the wall refuses before any change) and is removed once the probe has run. A descriptor
    held at the wall with an owner for signals or the asynchronous flag set is named in the refusal
    (``descriptor_signals.<number>``), so the host loads nothing: through it a plugin could make the kernel
    signal another process. A descriptor whose owner or flags cannot be read is named as well
    (``descriptor_unreadable.<number>``): the start is refused, never taken on trust."""
    canary, folder, remove = _canary() if sys.platform.startswith("linux") else ("", "", None)
    refused, left = "", []
    unreadable = []
    signals = _signal_descriptors(unreadable) if sys.platform.startswith("linux") else []
    try:
        try:
            wall_linux.apply_wall(_install_paths())
        except wall_linux.WallRefused as exc:
            refused = exc.code
        except Exception as exc:  # a path that cannot be opened, or any other failure: no wall is in place
            refused = "apply_wall_" + type(exc).__name__
        try:
            record = wall_linux.probe_wall(canary, os.getppid(), write_file=canary or None,
                                           write_folder=folder or None)
        except Exception:  # wall_system_unsupported, or a probe that cannot run: nothing is proven
            record = None
    finally:
        if remove is not None:
            try:
                left = remove()
            except OSError:
                left = ["unknown"]
    if signals or unreadable:  # a descriptor through which the walled host could (or might) make the kernel signal
        refused = ", ".join(filter(None, [refused] + [SIGNAL_DESCRIPTOR + "." + name for name in signals]
                                   + [UNREADABLE_DESCRIPTOR + "." + name for name in unreadable]))
    if left:  # the canary or the write folder is still there: a plugin would not find its folder as it was
        refused = ", ".join(filter(None, [refused, CANARY_LEFT]))
    return record, refused


# The refusal named when the canary or the write folder is still there after the helper ended.
CANARY_LEFT = "canary_left"
# The refusal named, with the descriptor's number after a dot, for a descriptor held at the wall that could
# make the kernel signal another process (see _signal_descriptors).
SIGNAL_DESCRIPTOR = "descriptor_signals"
# The refusal named, with the descriptor's number after a dot ("unknown" when the descriptors cannot be listed),
# for a descriptor held at the wall whose owner for signals or flags cannot be read.
UNREADABLE_DESCRIPTOR = "descriptor_unreadable"
_SIGNAL_TEXT = (
    "The plugin host held a descriptor ({descriptors}) that can make the kernel send a signal to another "
    "process (an owner for signals or the asynchronous flag was set on it before the host shut itself in), "
    "and the sandbox does not keep a plugin from using it, so nothing was loaded. What you can do: start the "
    "plugin host as the daemon does; whatever starts it must not set an owner for signals of any kind (a "
    "process, a process group or a thread: F_SETOWN or F_SETOWN_EX) or the asynchronous flag (O_ASYNC) on a "
    "descriptor it hands to the host.")
_UNREADABLE_TEXT = (
    "The plugin host could not read who gets the signals of a descriptor it held ({descriptors}), or its flags, "
    "so it could not tell whether a plugin could make the kernel send a signal to another process through it, "
    "and nothing was loaded. What you can do: start the plugin host as the daemon does, on a Linux kernel that "
    "reads a descriptor's owner with its kind (F_GETOWN_EX, Linux 2.6.32 and later); whatever starts it must "
    "hand it only descriptors it can read.")
_FILTER_ONLY_TEXT = (
    "The wall's record says that its filter, not Landlock, refused {what}, on a kernel whose Landlock (ABI "
    "{abi}) refuses that by itself, so the record shows the filter and not the part of the sandbox that holds "
    "files, and nothing was loaded. What you can do: start the plugin host again; if it is still refused, keep "
    "this message, as the host's own proof never gives such a record.")


def _wall_missing(record, refused):
    """What keeps the wall from being proven: the refusal, then each check of the record that the kernel
    did not refuse, then 'proven' when the record still does not say so. Empty only for a proven wall."""
    missing = [refused] if refused else []
    if type(record) is not dict:
        return missing + ["no_record"]
    missing += [name for name in wall_linux.RECORD_CHECKS if record.get(name) is not True]
    writes = record.get("writes") if isinstance(record.get("writes"), dict) else {}
    abi = record.get("landlock_abi")
    missing += ["writes." + kind for kind in wall_linux.WRITE_KINDS
                if writes.get(kind) not in _refused_by_wall(kind, abi)]
    if record.get("proven") is not True:
        missing.append("proven")
    return missing


def _refused_by_wall(kind, abi):
    """The outcomes of ``kind`` that show this wall refused it, at the Landlock ABI ``abi`` the record names.

    Where Landlock handles a kind by itself, only Landlock's refusal shows the wall: the filter holds
    truncation only, and only below the ABI that first handles it (wall_linux._TRUNCATE_ABI, a fact of the
    kernel's interface, not a setting). A record that credits the filter anywhere else shows the filter and
    not the part of the wall that holds files. An ABI that is not a whole number reads as one that handles
    truncation (the stricter reading)."""
    below = type(abi) is int and abi < wall_linux._TRUNCATE_ABI
    if kind == "truncate" and below:
        return wall_linux.WRITE_OUTCOMES[:2]
    return wall_linux.WRITE_OUTCOMES[:1]


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
        return hc.refusal(message, hc.ChannelRefusal(WALL_UNPROVEN, _unproven_detail(self._wall_record,
                                                                                      self._wall_missing)))

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


def _unproven_detail(record, missing):
    """The detail of a load refused for the wall: the sentences a person reads (wall_linux.WORDS), as many as
    fit, then the names of what is missing, which always fit and come last."""
    if type(record) is not dict:  # no record at all (no Linux, or a probe that could not run): the short form
        return ("the wall around this host is not proven, so it loads nothing and runs no plugin code; "
                "missing: " + ", ".join(missing))
    words = wall_linux.why_not_proven(record) or [wall_linux.WORDS["not_loaded"]]
    if CANARY_LEFT in ", ".join(missing):
        words.insert(1, wall_linux.WORDS["canary_left"])
    for code, text in ((UNREADABLE_DESCRIPTOR, _UNREADABLE_TEXT), (SIGNAL_DESCRIPTOR, _SIGNAL_TEXT)):
        named = [name.split(".", 1)[1] for name in ", ".join(missing).split(", ") if name.startswith(code + ".")]
        if named:
            words.insert(1, text.format(descriptors=", ".join(named)))
    writes = record.get("writes") if isinstance(record.get("writes"), dict) else {}
    filtered = [kind for kind in wall_linux.WRITE_KINDS
                if "writes." + kind in missing and writes.get(kind) == "refused_by_filter"]
    if filtered:
        what = (wall_linux.WORDS[filtered[0]] if len(filtered) == 1
                else "%d kinds of write, each named at the end" % len(filtered))
        words.insert(1, _FILTER_ONLY_TEXT.format(what=what, abi=record.get("landlock_abi")))
    tail = " Missing: " + ", ".join(missing)
    room = _DETAIL_MAX - len(tail)
    kept = []
    for i, line in enumerate(words):
        rest = len(words) - i - 1
        cost = len(" ".join(kept + [line]))
        if cost + (len(" " + wall_linux.WORDS["more"]) if rest else 0) > room:
            kept.append(wall_linux.WORDS["more"])
            break
        kept.append(line)
    text = " ".join(kept)
    return (text[:room] + tail)[-_DETAIL_MAX:] if len(text) > room else text + tail


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
        # The wall comes after the inherited descriptors are shut and the standard ones point at the null
        # device, and before the first message is read or sent: nothing the daemon sends reaches it.
        wall = _wall()
        program = _Program(channel, GilProbe() if gil_probe is None else gil_probe, wall)
        program.serve()
    return program.status()
