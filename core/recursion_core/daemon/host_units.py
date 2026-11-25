"""Plugin hosts as run units on Linux: one host per plugin, ended by the system's kill.

Each plugin host (``python -I -c START_CODE <core folder>``, the code of ``recursion_core.plugin_host``) is one transient
unit in the operator's user manager, started by ``EngineUnitManager.start_piped``: the launcher's own unit path
(``KillMode=control-group``, ``Delegate=yes``, the #916 ``STOP_PROPERTIES``, the argv passed verbatim) with ``--pipe``.
The host reads no variable of its environment, so the folder that holds Recursion's own code is handed to it as an
argument: by default the folder this daemon's own code was imported from.

* The channel is ONE socket pair (``AF_UNIX``, ``SOCK_STREAM``). One end is the unit's standard input AND standard
  output, and the daemon closes its own copy of that end as soon as systemd-run holds it. systemd-run stays running
  for the host's whole life and ends with it, so once the host has ended no copy of its end is left: the daemon's
  next read sees the end of the channel and its next send fails at once (``EPIPE``), and a send that was waiting on
  a host that read nothing more is released the same way.
* Stop of a host is ``cgroup.kill`` of its unit (by pidfd where the kernel has no ``cgroup.kill``), never a message
  to it. It waits on nothing the host does: a host that loops and swallows every exception and signal ends the same
  way as any other.
* Nothing watches a host between calls. A host that has ended by itself is found by the next call into it, and that
  call fails with ``ended``; a host that has not answered a call by its deadline is ended, and that call fails with
  ``timed_out``. Either way the call fails at once, within its own deadline, its plugin is marked degraded, and a
  new unit is started for it with the plugin's next call, inside that call's deadline, with the package of its last
  start. The mark goes when the plugin answers a call again, as the in-process operator deadline in ``plugins.py``
  does for a plugin in the daemon's process.
* The daemon's own time on a message of the host (a slow core request, say) does not count against the call's
  deadline: the deadline moves on by that time. And before a call is said to have timed out, a frame the host had
  already written is read without waiting and taken, so a host that answered in time is never ended as timed out.
* A call whose own message the channel refuses (an argument name it does not take, say) is not sent: it fails with
  ``CallRefused`` (``call_invalid``), and the host is not touched. ``ended`` is said only of what the host's end
  does; an answer of the daemon's that the channel refuses (a core answer over the frame limit, say) reaches the
  host as a refusal of its message, and the call goes on.
* A second ``start`` of a plugin ends its running host before it waits for the plugin's turn, as ``stop`` does: a
  call that waits on that host (one with no deadline, say) ends at once with ``replaced``, and so does a start of
  the plugin still under way. Then the new host starts.
* ``stop`` and ``close`` (the daemon's own Stop) end every host the same way and wait on none, a host that is still
  starting included: its start ends with ``stopped``.

Not here: which plugins go to a host (the daemon still runs every plugin in its own process), the wall around a
host, and handing a descriptor to a host.
"""
from __future__ import annotations

import itertools
import logging
import math
import os
from pathlib import Path
import re
import reprlib
import secrets
import select
import socket
import sys
import threading
import time

from recursion_core.control_transport.errors import FrameError
from recursion_core.control_transport.framing import HEADER
from recursion_core.plugin_host import DEFAULT_GIL_CHECK, START_CODE, START_REFUSED
from recursion_core.plugin_security import host_channel as hc

from .launcher import _unit_path_ok, check_piped_properties, private_dir, require_launcher_platform
from .plugins import OPERATOR_DEADLINE_S

log = logging.getLogger(__name__)

# Each of these two becomes a row of the person's own settings file, read through its one reader. Until that file is
# at this base each stands here once, as its shipped default. None means no deadline; any other value is a number of
# seconds above zero, and a number too large for a float is a wait that no clock reaches. Any other value is refused
# when the PluginHostUnits is made, before anything starts.
# plugin_host.call_deadline_s: how long a call into a host may go unanswered before the host is ended and started
# again. Shipped default: the deadline of an operator call into a plugin in the daemon's process.
CALL_DEADLINE_S = OPERATOR_DEADLINE_S
# plugin_host.start_deadline_s: how long a new host may take to say hello. Shipped default: the engine launcher's
# own start wait.
START_DEADLINE_S = 10.0

# The longest single wait the platform takes (Python's own figure, not a setting): a longer deadline is waited in
# slices of it. poll takes at most a C int of milliseconds (the platform's own figure, not a setting).
_LONGEST_WAIT_S = threading.TIMEOUT_MAX
_LONGEST_POLL_S = (2 ** 31 - 1) / 1000

HOST_UNIT = re.compile(r'recursion-host-[0-9a-f]{16}\.service')
# The interpreter and this argv start every host, and the core folder comes last. No switch forces the interpreter's
# lock off: a free-threaded build starts with it off, and only without such a switch does an extension that is not
# marked safe without the lock turn it on where the host's own check sees it (plugin_security/runtime.py).
HOST_ARGV = ('-I', '-c', START_CODE)
# The folder that holds the recursion_core this daemon runs: the one a host is handed unless another is given.
CORE_FOLDER = str(Path(__file__).resolve().parents[2])
STOPPED, ENDED, TIMED_OUT, NOT_STARTED = 'stopped', 'ended', 'timed_out', 'not_started'
# A host that a new start of its plugin ended: what a call or a start on it ends with. Never counted as degraded.
REPLACED = 'replaced'
# A call the daemon did not send because the channel refuses the daemon's own message (CallRefused).
CALL_INVALID = 'call_invalid'
# What a person reads when a call got no answer from its host, by the reason.
_NO_ANSWER = {TIMED_OUT: 'did not answer the call by its deadline', ENDED: 'ended during the call'}


def _deadline(name, value):
    """Seconds as a float, or None for no deadline. A whole number too large for a float waits like infinity."""
    if value is None:
        return None
    number = isinstance(value, (int, float)) and not isinstance(value, bool)
    if not number or value != value or value <= 0 or value == math.inf:
        raise ValueError(f'{name} = {reprlib.repr(value)} is not a deadline: write a number of seconds above zero, '
                         'or None for no deadline')
    try:
        return float(value)
    except OverflowError:
        return math.inf  # more seconds than a float holds: waited in slices, so no clock ever reaches it


def _core_folder(value):
    """The folder a host is handed for Recursion's own code: an absolute path of a folder, as a string."""
    folder = os.fspath(value) if isinstance(value, (str, os.PathLike)) else None
    if not isinstance(folder, str) or not os.path.isabs(folder) or not os.path.isdir(folder):
        raise ValueError(f'core_folder = {reprlib.repr(value)} is not a folder a plugin host can be given: write the '
                         'absolute path of the folder that holds recursion_core')
    return folder


def host_argv(python, core_folder):
    """The argv of one host: the interpreter, HOST_ARGV and the folder of Recursion's own code."""
    return [python, *HOST_ARGV, core_folder]


def _ready(sock, writing, deadline):
    """True once ``sock`` can be written (or read); False once ``deadline`` has passed first.

    By poll, which takes a descriptor of any number. An error of the wait itself is an OSError, which every caller
    takes as the end of that host."""
    try:
        poller = select.poll()
        poller.register(sock, select.POLLOUT if writing else select.POLLIN)
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            if poller.poll(math.ceil(min(left, _LONGEST_WAIT_S, _LONGEST_POLL_S) * 1000)):
                return True
    except (ValueError, OverflowError, TypeError) as exc:
        raise OSError(f'the wait on the host channel failed: {exc}') from None


def _reply_frame(received, reply):
    """The frame of the daemon's answer to one message of the host. An answer the channel refuses (a core answer
    over the frame limit, say) is the daemon's own fault, never the host's: the host gets a refusal of its message
    instead, with the channel's code, and the call goes on."""
    try:
        return hc.encode(hc.check_message(reply, receiver='host'))
    except hc.ChannelRefusal as exc:
        refused = exc
    except (FrameError, ValueError, TypeError) as exc:
        refused = hc.ChannelRefusal('message_invalid', f'the daemon could not write its answer: {exc}')
    return hc.encode(hc.check_message(hc.refusal(received, refused), receiver='host'))


class HostUnavailable(Exception):
    """A call or start that got no answer from its host. ``reason``: stopped, replaced, ended, timed_out or
    not_started."""

    def __init__(self, plugin_id, reason, detail=''):
        super().__init__(f'{plugin_id}: {reason}' + (f': {detail}' if detail else ''))
        self.plugin_id, self.reason, self.detail = plugin_id, reason, detail


class CallRefused(ValueError):
    """A call the daemon did not send, because the channel refuses the daemon's own message for it.

    ``reason`` is ``call_invalid``; ``code`` and ``detail`` are the channel's refusal (an argument name it does not
    take, say). The host was not touched: it keeps running, is not marked degraded and answers the next call."""

    def __init__(self, plugin_id, code, detail=''):
        super().__init__(f'{plugin_id}: {CALL_INVALID}: the call was not sent, because the host channel refuses it ('
                         + code + (f': {detail}' if detail else '') + '). The host is still running: correct the '
                         'call and send it again')
        self.plugin_id, self.reason, self.code, self.detail = plugin_id, CALL_INVALID, code, detail


class _HostWentAway(Exception):
    def __init__(self, reason, detail=''):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


class HostUnit:
    """One host unit: its name, its cgroup, the systemd-run process that holds its channel, and the daemon's end."""

    def __init__(self, plugin_id, unit, sock, process, end):
        self.plugin_id, self.unit, self.group = plugin_id, unit, ''
        self.end = end  # host_channel.DaemonEnd: answers what the host asks of the core
        self.ended = self.stopped = self.replaced = False
        self.refusals = []  # refusals that answered no call (a load the host refused, say)
        self._sock, self._process = sock, process
        self._buffer = b''
        self._at_end = False  # the end of the channel was read by a read that did not wait

    def send(self, message, deadline=None):
        self.send_frame(hc.encode(hc.check_message(message, receiver='host')), deadline)

    def send_frame(self, frame, deadline=None):
        """Write one frame. With no deadline, wait as long as the host takes to read it."""
        if deadline is None:
            self._sock.sendall(frame)
            return
        view = memoryview(frame)
        while view:
            if not _ready(self._sock, True, deadline):
                raise TimeoutError('the host did not read its channel')
            try:
                view = view[self._sock.send(view, socket.MSG_DONTWAIT):]
            except BlockingIOError:
                continue

    def receive(self, deadline=None):
        """The next message from the host, or None at the end of the channel; TimeoutError past ``deadline``.

        Before it says the deadline has passed it reads, without waiting, what the host had already written: a
        frame that was on the channel in time is taken, never lost to the timeout."""
        while True:
            if len(self._buffer) >= HEADER.size:
                (length,) = HEADER.unpack_from(self._buffer)
                if length > hc.frame_limit('host'):
                    raise hc.ChannelRefusal('message_invalid', 'frame_too_large')
                if len(self._buffer) >= HEADER.size + length:
                    body = self._buffer[HEADER.size:HEADER.size + length]
                    self._buffer = self._buffer[HEADER.size + length:]
                    return hc.decode(body, sender='host')
            if self._at_end:
                return None
            if deadline is not None and not _ready(self._sock, False, deadline):
                if not self._read_written():
                    raise TimeoutError('the host did not answer')
                continue
            chunk = self._sock.recv(1 << 16)
            if not chunk:
                return None
            self._buffer += chunk

    def _read_written(self):
        """Read what the host has already written, without waiting, up to one whole frame. True if anything came."""
        came = False
        while True:
            if len(self._buffer) >= HEADER.size:
                (length,) = HEADER.unpack_from(self._buffer)
                if length > hc.frame_limit('host') or len(self._buffer) >= HEADER.size + length:
                    return came  # one whole frame (or one too large to read): receive takes it from here
            try:
                chunk = self._sock.recv(1 << 16, socket.MSG_DONTWAIT)
            except BlockingIOError:
                return came
            if not chunk:
                self._at_end = True
                return True
            self._buffer += chunk
            came = True

    def start_refusal(self):
        """The host's refusal to start (``host_start_refused``) when it has sent one already, else None.

        Waits on nothing and keeps every frame it read for ``receive``."""
        try:
            while chunk := self._sock.recv(1 << 16, socket.MSG_DONTWAIT):
                self._buffer += chunk
        except OSError:
            pass
        view = self._buffer
        while len(view) >= HEADER.size:
            (length,) = HEADER.unpack_from(view)
            if length > hc.frame_limit('host') or len(view) < HEADER.size + length:
                return None
            try:
                message = hc.decode(view[HEADER.size:HEADER.size + length], sender='host')
            except hc.ChannelRefusal:
                return None
            if (type(message) is dict and message.get('type') == 'refusal' and message.get('code') == START_REFUSED
                    and type(message.get('detail')) is str):
                return message
            view = view[HEADER.size + length:]
        return None

    def wake(self):
        """Wake a thread of the daemon's that waits on this channel: it reads the end of the channel at once."""
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass
        if self._process.poll() is None:
            log.info('systemd-run for %s is still running after its unit was ended', self.unit)


class PluginHostUnits:
    """The daemon's plugin hosts on Linux: ``start``, ``call``, ``stop`` and ``close``, one host per plugin."""

    def __init__(self, manager, state_dir, core, *, python=None, gil_check=DEFAULT_GIL_CHECK,
                 call_deadline_s=CALL_DEADLINE_S, start_deadline_s=START_DEADLINE_S, properties=(), core_folder=None):
        require_launcher_platform()
        self._manager, self._state, self._core = manager, Path(state_dir), core
        self._python = python or sys.executable
        self._core_folder = _core_folder(CORE_FOLDER if core_folder is None else core_folder)
        self._gil_check = gil_check
        self._call_deadline_s = _deadline('call_deadline_s', call_deadline_s)
        self._start_deadline_s = _deadline('start_deadline_s', start_deadline_s)
        self._properties = check_piped_properties(properties)
        self._lock = threading.Lock()
        self._turns = {}       # plugin id -> lock: one call at a time on a host's channel
        self._packages = {}    # plugin id -> (manifest, files): what a restart loads again
        self._units = {}       # plugin id -> the running HostUnit
        self._starting = {}    # plugin id -> the HostUnit whose start is under way
        self._replacements = {}  # plugin id -> how many starts have replaced its host: a start begun before is late
        self._stopped = set()  # plugin ids whose host was stopped: not started again by a call
        self._ended = []       # HostUnits ended and not yet closed
        self._ids = itertools.count(1)
        self._closed = False
        self.degraded = {}     # plugin id -> calls that ended with no answer since the last answer

    # -- the public calls ----------------------------------------------------------------------
    def start(self, plugin_id, manifest, files):
        """Start the host of ``plugin_id`` and load its package (checked before, as bytes). Returns its HostUnit.

        A host the plugin already has is ended BEFORE this start waits for the plugin's turn, as ``stop`` does: a call
        that waits on that host (one with no deadline, say) ends at once with ``replaced`` and gives the turn up, and
        a start of the plugin still under way ends with ``replaced`` too."""
        package = (bytes(manifest), dict(files))
        with self._lock:
            if self._closed:
                raise HostUnavailable(plugin_id, STOPPED, 'the daemon is closing')
            self._stopped.discard(plugin_id)
            turn = self._turns.setdefault(plugin_id, threading.Lock())
            self._replacements[plugin_id] = self._replacements.get(plugin_id, 0) + 1
            running = self._units.pop(plugin_id, None)
            starting = self._starting.get(plugin_id)
        for host in (running, starting):
            if host is not None:
                self._replace(host)
        with turn:
            if running is not None:
                self._discard(running)  # this start holds the plugin's turn now: nothing else reads that channel
            with self._lock:
                self._packages[plugin_id] = package  # under the turn: the last start's package is what a call loads again
                old = self._units.pop(plugin_id, None)
            if old is not None:  # a host that started while this start waited for the turn
                self._replace(old)
                self._discard(old)
            return self._start(plugin_id, package)

    def unit_of(self, plugin_id):
        """The running HostUnit of ``plugin_id``, or None."""
        with self._lock:
            return self._units.get(plugin_id)

    def call(self, plugin_id, name, args):
        """Call one function of the plugin in its host. Returns the host's ``result`` or ``refusal`` message.

        No answer by the deadline, or the end of the host, ends the host and marks the plugin degraded, and the call
        raises HostUnavailable at once. A plugin whose host has ended gets a new one with its next call, started
        inside that call's deadline. A stopped host is not started again here.
        """
        message = {'type': 'call', 'id': next(self._ids), 'name': name, 'args': dict(args)}
        try:  # the daemon's own message, checked before anything is sent: a refusal here is the caller's
            frame = hc.encode(hc.check_message(message, receiver='host'))
        except hc.ChannelRefusal as exc:
            raise CallRefused(plugin_id, exc.code, exc.detail) from None
        except (FrameError, ValueError, TypeError) as exc:  # not written as one frame of JSON: too large, a set, NaN
            raise CallRefused(plugin_id, 'message_invalid', f'{type(exc).__name__}: {exc}') from None
        with self._lock:
            turn = self._turns.setdefault(plugin_id, threading.Lock())
        with turn:
            with self._lock:
                host = self._units.get(plugin_id)
                if self._closed or plugin_id in self._stopped:
                    raise HostUnavailable(plugin_id, STOPPED)
                package = self._packages.get(plugin_id)
                if package is None:
                    raise HostUnavailable(plugin_id, NOT_STARTED, 'no host was started for this plugin')
            deadline = None if self._call_deadline_s is None else time.monotonic() + self._call_deadline_s
            if host is None:
                host = self._start(plugin_id, package, deadline)
            try:
                answer = self._exchange(host, message, frame, deadline)
            except _HostWentAway as gone:
                reason = REPLACED if host.replaced else STOPPED if host.stopped else gone.reason
                self._end(host)
                self._discard(host)  # this call holds the plugin's turn: nothing else reads this channel now
                if reason not in (STOPPED, REPLACED):
                    with self._lock:
                        self.degraded[plugin_id] = self.degraded.get(plugin_id, 0) + 1
                    log.error('plugin %s: its host %s %s and was ended. The call was not answered, so it may be sent '
                              'again; a new host is started for the plugin with its next call', plugin_id, host.unit,
                              _NO_ANSWER.get(reason, reason))
                raise HostUnavailable(plugin_id, reason, gone.detail) from None
            with self._lock:
                self.degraded.pop(plugin_id, None)
            return answer

    def stop(self, plugin_id):
        """End the host of ``plugin_id`` by the system's kill of its unit. Sends it nothing and waits on nothing."""
        with self._lock:
            self._stopped.add(plugin_id)
            host = self._units.pop(plugin_id, None)
            starting = self._starting.get(plugin_id)
        if host is not None:
            host.stopped = True
            self._end(host)
        if starting is not None:
            self._end_start(starting)

    def close(self):
        """The daemon's own Stop: every host is ended by the system's kill; none is waited on."""
        with self._lock:
            self._closed = True
            hosts = list(self._units.values())
            self._units.clear()
            starting = list(self._starting.values())
        for host in hosts:
            host.stopped = True
            self._end(host)
        for host in starting:
            self._end_start(host)
        with self._lock:
            ended, self._ended = self._ended, []
        for host in ended:
            turn = self._turns.get(host.plugin_id)
            if turn is None or turn.acquire(blocking=False):
                try:
                    host.close()
                finally:
                    if turn is not None:
                        turn.release()
            else:
                with self._lock:
                    self._ended.append(host)  # a call still holds it; that call closes it

    # -- inside ------------------------------------------------------------------------------
    def _start(self, plugin_id, package, limit=None):
        """Start one host unit and load ``package``. The caller holds the plugin's turn.

        ``limit``: a deadline of the caller's (a call's own) that the start does not pass, whatever the start
        deadline is."""
        manifest, files = package
        with self._lock:
            replacements = self._replacements.get(plugin_id, 0)  # a start of the plugin after this makes it late
        end = hc.DaemonEnd(plugin_id, self._core)
        load = end.load(plugin_id, manifest, files)
        token = secrets.token_hex(8)
        unit = f'recursion-host-{token}.service'
        if not HOST_UNIT.fullmatch(unit):
            raise HostUnavailable(plugin_id, NOT_STARTED, 'unit name')
        folder = private_dir(self._state / 'plugin-hosts' / token)  # the host's empty working folder
        if not _unit_path_ok(folder):
            raise HostUnavailable(plugin_id, NOT_STARTED, 'state_path_not_unit_safe')
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            process = self._manager.start_piped(unit, host_argv(self._python, self._core_folder), theirs,
                                                properties=self._properties, working_directory=folder)
        except BaseException:
            ours.close()
            raise
        finally:
            theirs.close()  # the daemon keeps no copy of the host's end
        host = HostUnit(plugin_id, unit, ours, process, end)
        with self._lock:
            self._starting[plugin_id] = host  # from here stop and close see this host
            late = self._closed or plugin_id in self._stopped
            if self._replacements.get(plugin_id, 0) != replacements:
                host.replaced = late = True
        if late:
            self._end_start(host)
        deadline = None if self._start_deadline_s is None else time.monotonic() + self._start_deadline_s
        if limit is not None:
            deadline = limit if deadline is None else min(deadline, limit)
        try:
            hello = host.receive(deadline)
            if type(hello) is not dict or hello.get('type') != 'hello' or hello.get('channel') != hc.CHANNEL:
                raise HostUnavailable(plugin_id, NOT_STARTED, 'the host did not say hello')
            if host.start_refusal() is not None:  # a hello that came with the host's refusal to start
                raise HostUnavailable(plugin_id, NOT_STARTED, START_REFUSED)
            group, _ = self._manager.unit_identity(unit)
            if not group or Path(group).name != unit:
                raise HostUnavailable(plugin_id, NOT_STARTED, 'the unit has no control group')
            host.group = group
            host.send({'type': 'hello', 'channel': hc.CHANNEL, 'gil_check': self._gil_check}, deadline)
            host.send_frame(load, deadline)
        except BaseException as exc:
            refused = host.start_refusal() if isinstance(exc, (OSError, HostUnavailable)) else None
            with self._lock:
                self._starting.pop(plugin_id, None)
            self._end(host)
            self._discard(host)
            if host.replaced and isinstance(exc, Exception):
                raise HostUnavailable(plugin_id, REPLACED, 'a later start of the plugin replaced this host while it '
                                      'was starting; that start runs the plugin') from None
            if host.stopped and isinstance(exc, Exception):
                raise HostUnavailable(plugin_id, STOPPED, 'the host was stopped while it was starting; start the '
                                      'plugin again to run it') from None
            if refused is not None:
                log.error('plugin %s: its host %s refused to start, and no plugin code ran: %s', plugin_id, unit,
                          refused['detail'])
                raise HostUnavailable(plugin_id, NOT_STARTED, refused['detail']) from None
            if isinstance(exc, (OSError, hc.ChannelRefusal)):
                raise HostUnavailable(plugin_id, NOT_STARTED, type(exc).__name__) from None
            raise
        with self._lock:
            self._starting.pop(plugin_id, None)
            if not self._closed and plugin_id not in self._stopped and not host.stopped:
                self._units[plugin_id] = host
                return host
        host.stopped = True
        self._end(host)
        self._discard(host)
        if host.replaced:
            raise HostUnavailable(plugin_id, REPLACED, 'a later start of the plugin replaced this host while it was '
                                  'starting; that start runs the plugin')
        raise HostUnavailable(plugin_id, STOPPED)

    def _exchange(self, host, message, frame, deadline):
        """Send one call (``frame``, checked before) and read until its answer, answering what the host asks of the
        core on the way."""
        try:
            host.send_frame(frame, deadline)
            while True:
                received = host.receive(deadline)
                if received is None:
                    raise _HostWentAway(ENDED)
                kind = received.get('type') if type(received) is dict else None
                if kind in ('result', 'refusal') and received.get('id') == message['id']:
                    return hc.check_message(received, receiver='daemon')
                if kind == 'refusal' and received.get('id') is None:
                    host.refusals.append(received)
                    continue
                began = time.monotonic()
                reply = host.end.receive(received)
                reply = None if reply is None else _reply_frame(received, reply)
                if deadline is not None:
                    # The daemon's own time on the host's message (a slow core request, say) is not the host's: the
                    # call's deadline moves on by it, so a host is never ended for the daemon's slowness.
                    deadline += time.monotonic() - began
                if reply is not None:
                    host.send_frame(reply, deadline)
        except TimeoutError:
            raise _HostWentAway(TIMED_OUT) from None
        except hc.ChannelRefusal as exc:
            raise _HostWentAway(ENDED, exc.code) from None
        except OSError as exc:
            raise _HostWentAway(ENDED, type(exc).__name__) from None

    def _discard(self, host):
        """Close an ended host's channel. Only the holder of the plugin's turn (or ``close``) calls this."""
        with self._lock:
            if host in self._ended:
                self._ended.remove(host)
        host.close()

    def _replace(self, host):
        """End a host that a new start of its plugin replaces, and wake whoever waits on its channel."""
        host.replaced = host.stopped = True
        self._end(host)
        host.wake()

    def _end_start(self, host):
        """End a host whose start is under way and wake that start, which then ends with ``stopped``.

        A unit that shows no cgroup yet has run no plugin byte (the package is sent only after the cgroup check):
        its systemd-run is ended, and the host's own code ends at the end of its channel, which ``wake`` makes.
        """
        host.stopped = True
        self._end(host)
        host.wake()

    def _end(self, host):
        """SIGKILL the unit's whole cgroup now, once. Waits on nothing the host does."""
        with self._lock:
            first, host.ended = not host.ended, True
            if self._units.get(host.plugin_id) is host:
                del self._units[host.plugin_id]
            if first:
                self._ended.append(host)
        if not first:
            return
        group = host.group
        if not group:
            try:
                group, _ = self._manager.unit_identity(host.unit)
            except Exception:  # noqa: BLE001 - no cgroup known: the unit never ran
                group = ''
        if not (group and Path(group).name == host.unit and self._manager.kill_cgroup(group, host.unit)):
            if host._process.poll() is None:
                host._process.kill()  # our own systemd-run, whose unit never got a cgroup
        try:
            os.rmdir(self._state / 'plugin-hosts' / host.unit[len('recursion-host-'):-len('.service')])
        except OSError:
            pass
