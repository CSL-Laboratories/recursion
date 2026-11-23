"""Plugin hosts as run units on Linux: one host per plugin, ended by the system's kill.

Each plugin host (``python -X gil=0 -I -m recursion_core.plugin_host``) is one transient unit in the operator's user
manager, started by ``EngineUnitManager.start_piped``: the launcher's own unit path (``KillMode=control-group``,
``Delegate=yes``, the #916 ``STOP_PROPERTIES``, the argv passed verbatim) with ``--pipe``.

* The channel is ONE socket pair (``AF_UNIX``, ``SOCK_STREAM``). One end is the unit's standard input AND standard
  output, and the daemon closes its own copy of that end as soon as systemd-run holds it. systemd-run stays running
  for the host's whole life and ends with it, so once the host has ended no copy of its end is left: the daemon's
  next read sees the end of the channel and its next send fails at once (``EPIPE``), and a send that was waiting on
  a host that read nothing more is released the same way.
* Stop of a host is ``cgroup.kill`` of its unit (by pidfd where the kernel has no ``cgroup.kill``), never a message
  to it. It waits on nothing the host does: a host that loops and swallows every exception and signal ends the same
  way as any other.
* A host that ends by itself, or that has not answered a call by its deadline, is ended, its plugin is marked
  degraded, and a new unit is started for it at once with the same package. The mark goes when the plugin answers a
  call again, as the in-process operator deadline in ``plugins.py`` does for a plugin in the daemon's process.
* ``close`` (the daemon's own Stop) ends every host the same way and waits on none.

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
import secrets
import select
import socket
import sys
import threading
import time

from recursion_core.control_transport.framing import HEADER
from recursion_core.plugin_host import DEFAULT_GIL_CHECK
from recursion_core.plugin_security import host_channel as hc

from .launcher import _unit_path_ok, private_dir, require_launcher_platform
from .plugins import OPERATOR_DEADLINE_S

log = logging.getLogger(__name__)

# Each of these two becomes a row of the person's own settings file, read through its one reader. Until that file is
# at this base each stands here once, as its shipped default. None means no deadline; any other value is a number of
# seconds above zero.
# plugin_host.call_deadline_s: how long a call into a host may go unanswered before the host is ended and started
# again. Shipped default: the deadline of an operator call into a plugin in the daemon's process.
CALL_DEADLINE_S = OPERATOR_DEADLINE_S
# plugin_host.start_deadline_s: how long a new host may take to say hello. Shipped default: the engine launcher's
# own start wait.
START_DEADLINE_S = 10.0

HOST_UNIT = re.compile(r'recursion-host-[0-9a-f]{16}\.service')
HOST_ARGV = ('-X', 'gil=0', '-I', '-m', 'recursion_core.plugin_host')
STOPPED, ENDED, TIMED_OUT, NOT_STARTED = 'stopped', 'ended', 'timed_out', 'not_started'


def _deadline(name, value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f'{name} is a number of seconds above zero, or None for no deadline')
    return float(value)


class HostUnavailable(Exception):
    """A call or start that got no answer from its host. ``reason``: stopped, ended, timed_out or not_started."""

    def __init__(self, plugin_id, reason, detail=''):
        super().__init__(f'{plugin_id}: {reason}' + (f': {detail}' if detail else ''))
        self.plugin_id, self.reason, self.detail = plugin_id, reason, detail


class _HostWentAway(Exception):
    def __init__(self, reason, detail=''):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


class HostUnit:
    """One host unit: its name, its cgroup, the systemd-run process that holds its channel, and the daemon's end."""

    def __init__(self, plugin_id, unit, sock, process, end):
        self.plugin_id, self.unit, self.group = plugin_id, unit, ''
        self.end = end  # host_channel.DaemonEnd: answers what the host asks of the core
        self.ended = self.stopped = False
        self.refusals = []  # refusals that answered no call (a load the host refused, say)
        self._sock, self._process = sock, process
        self._buffer = b''

    def send(self, message, deadline=None):
        self.send_frame(hc.encode(hc.check_message(message, receiver='host')), deadline)

    def send_frame(self, frame, deadline=None):
        """Write one frame. With no deadline, wait as long as the host takes to read it."""
        if deadline is None:
            self._sock.sendall(frame)
            return
        view = memoryview(frame)
        while view:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([], [self._sock], [], left)[1]:
                raise TimeoutError('the host did not read its channel')
            try:
                view = view[self._sock.send(view, socket.MSG_DONTWAIT):]
            except BlockingIOError:
                continue

    def receive(self, deadline=None):
        """The next message from the host, or None at the end of the channel; TimeoutError past ``deadline``."""
        while True:
            if len(self._buffer) >= HEADER.size:
                (length,) = HEADER.unpack_from(self._buffer)
                if length > hc.frame_limit('host'):
                    raise hc.ChannelRefusal('message_invalid', 'frame_too_large')
                if len(self._buffer) >= HEADER.size + length:
                    body = self._buffer[HEADER.size:HEADER.size + length]
                    self._buffer = self._buffer[HEADER.size + length:]
                    return hc.decode(body, sender='host')
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and (left <= 0 or not select.select([self._sock], [], [], left)[0]):
                raise TimeoutError('the host did not answer')
            chunk = self._sock.recv(1 << 16)
            if not chunk:
                return None
            self._buffer += chunk

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
                 call_deadline_s=CALL_DEADLINE_S, start_deadline_s=START_DEADLINE_S, properties=()):
        require_launcher_platform()
        self._manager, self._state, self._core = manager, Path(state_dir), core
        self._python = python or sys.executable
        self._gil_check = gil_check
        self._call_deadline_s = _deadline('call_deadline_s', call_deadline_s)
        self._start_deadline_s = _deadline('start_deadline_s', start_deadline_s)
        self._properties = tuple(properties)
        self._lock = threading.Lock()
        self._turns = {}       # plugin id -> lock: one call at a time on a host's channel
        self._packages = {}    # plugin id -> (manifest, files): what a restart loads again
        self._units = {}       # plugin id -> the running HostUnit
        self._stopped = set()  # plugin ids whose host was stopped: not started again by a call
        self._ended = []       # HostUnits ended and not yet closed
        self._ids = itertools.count(1)
        self._closed = False
        self.degraded = {}     # plugin id -> calls that ended with no answer since the last answer

    # -- the public calls ----------------------------------------------------------------------
    def start(self, plugin_id, manifest, files):
        """Start the host of ``plugin_id`` and load its package (checked before, as bytes). Returns its HostUnit."""
        with self._lock:
            if self._closed:
                raise HostUnavailable(plugin_id, STOPPED, 'the daemon is closing')
            self._stopped.discard(plugin_id)
            self._packages[plugin_id] = (bytes(manifest), dict(files))
            turn = self._turns.setdefault(plugin_id, threading.Lock())
        with turn:
            return self._start(plugin_id)

    def unit_of(self, plugin_id):
        """The running HostUnit of ``plugin_id``, or None."""
        with self._lock:
            return self._units.get(plugin_id)

    def call(self, plugin_id, name, args):
        """Call one function of the plugin in its host. Returns the host's ``result`` or ``refusal`` message.

        No answer by the deadline, or the end of the host, ends the host, marks the plugin degraded and starts a new
        host for it; the call raises HostUnavailable. A stopped host is not started again here.
        """
        with self._lock:
            turn = self._turns.setdefault(plugin_id, threading.Lock())
        with turn:
            with self._lock:
                host = self._units.get(plugin_id)
                if self._closed or plugin_id in self._stopped:
                    raise HostUnavailable(plugin_id, STOPPED)
                if plugin_id not in self._packages:
                    raise HostUnavailable(plugin_id, NOT_STARTED, 'no host was started for this plugin')
            if host is None:
                host = self._start(plugin_id)
            id_ = next(self._ids)
            deadline = None if self._call_deadline_s is None else time.monotonic() + self._call_deadline_s
            try:
                answer = self._exchange(host, {'type': 'call', 'id': id_, 'name': name, 'args': dict(args)}, deadline)
            except _HostWentAway as gone:
                reason = STOPPED if host.stopped else gone.reason
                self._end(host)
                self._discard(host)  # this call holds the plugin's turn: nothing else reads this channel now
                if reason != STOPPED:
                    with self._lock:
                        self.degraded[plugin_id] = self.degraded.get(plugin_id, 0) + 1
                    log.error('plugin host %s of %s %s; marked degraded and started again', host.unit, plugin_id,
                              reason)
                    self._restart(plugin_id)
                raise HostUnavailable(plugin_id, reason, gone.detail) from None
            with self._lock:
                self.degraded.pop(plugin_id, None)
            return answer

    def stop(self, plugin_id):
        """End the host of ``plugin_id`` by the system's kill of its unit. Sends it nothing and waits on nothing."""
        with self._lock:
            self._stopped.add(plugin_id)
            host = self._units.pop(plugin_id, None)
        if host is not None:
            host.stopped = True
            self._end(host)

    def close(self):
        """The daemon's own Stop: every host is ended by the system's kill; none is waited on."""
        with self._lock:
            self._closed = True
            hosts = list(self._units.values())
            self._units.clear()
        for host in hosts:
            host.stopped = True
            self._end(host)
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
    def _start(self, plugin_id):
        """Start one host unit and load the package. The caller holds the plugin's turn."""
        manifest, files = self._packages[plugin_id]
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
            process = self._manager.start_piped(unit, [self._python, *HOST_ARGV], theirs,
                                                properties=self._properties, working_directory=folder)
        except BaseException:
            ours.close()
            raise
        finally:
            theirs.close()  # the daemon keeps no copy of the host's end
        host = HostUnit(plugin_id, unit, ours, process, end)
        deadline = None if self._start_deadline_s is None else time.monotonic() + self._start_deadline_s
        try:
            hello = host.receive(deadline)
            if type(hello) is not dict or hello.get('type') != 'hello' or hello.get('channel') != hc.CHANNEL:
                raise HostUnavailable(plugin_id, NOT_STARTED, 'the host did not say hello')
            group, _ = self._manager.unit_identity(unit)
            if not group or Path(group).name != unit:
                raise HostUnavailable(plugin_id, NOT_STARTED, 'the unit has no control group')
            host.group = group
            host.send({'type': 'hello', 'channel': hc.CHANNEL, 'gil_check': self._gil_check}, deadline)
            host.send_frame(load, deadline)
        except (OSError, hc.ChannelRefusal) as exc:
            self._end(host)
            self._discard(host)
            raise HostUnavailable(plugin_id, NOT_STARTED, type(exc).__name__) from None
        except BaseException:
            self._end(host)
            self._discard(host)
            raise
        with self._lock:
            if not self._closed and plugin_id not in self._stopped:
                self._units[plugin_id] = host
                return host
        host.stopped = True
        self._end(host)
        self._discard(host)
        raise HostUnavailable(plugin_id, STOPPED)

    def _restart(self, plugin_id):
        with self._lock:
            self._units.pop(plugin_id, None)
            if self._closed or plugin_id in self._stopped:
                return
        try:
            self._start(plugin_id)
        except Exception:  # noqa: BLE001 - the next call starts it again
            log.exception('plugin host of %s could not be started again', plugin_id)

    def _exchange(self, host, message, deadline):
        """Send one call and read until its answer, answering what the host asks of the core on the way."""
        try:
            host.send(message, deadline)
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
                reply = host.end.receive(received)
                if reply is not None:
                    host.send(reply, deadline)
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
