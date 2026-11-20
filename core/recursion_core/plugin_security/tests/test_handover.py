"""Tests of plugin_security/handover.py: a connected socket handed to a plugin host and cut again.

Every connection here is to a server of the test on 127.0.0.1 (port picked by the system).
The hosts are a plain child of the test and, where a user service manager answers, units
started with ``systemd-run --user --pipe`` named orderstest-*, which each test stops and
shows gone. Every file lives in a temporary folder the test made.
"""

from __future__ import annotations

import array
import inspect
import json
import os
import select
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    import fcntl
except ImportError:  # not on Windows
    fcntl = None

from recursion_core.plugin_security import Refused
from recursion_core.plugin_security import handover
from recursion_core.plugin_security.handover import (
    HANDED, HANDOVER_SEND_TIMEOUT_S, HOST_SLOW, NOT_SHOWN, PLATFORM_CODE, REFUSED_CODE, HandoverTable,
    handover_shown, receive_handover, require_handover_platform,
)

CORE = str(Path(__file__).resolve().parents[3])
LINUX = handover_shown()
PAYLOAD = b'{"handover": 1}\n'

HOST_SCRIPT = textwrap.dedent('''\
    import errno, fcntl, json, os, socket, subprocess, sys, time
    sys.path.insert(0, sys.argv[1])
    from recursion_core.plugin_security.handover import receive_handover
    ch = socket.socket(fileno=0)
    def say(**kw):
        try:
            ch.sendall((json.dumps(kw) + chr(10)).encode())
        except OSError:
            pass
    data, s = receive_handover(ch, 4096, timeout=5.0)
    if s is None:
        say(got=None)
        sys.exit(0)
    fd = s.fileno()
    probe = "import os" + chr(10) + "try:" + chr(10) + " os.fstat(%d); print('open')" % fd + chr(10) + "except OSError: print('closed')"
    child = subprocess.run([sys.executable, "-c", probe], close_fds=False, capture_output=True, text=True)
    s.sendall(b"ping-1")
    echo = s.recv(6)
    say(got=data.decode(), echo=echo.decode(), child=child.stdout.strip(),
        cloexec=bool(fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC), inheritable=os.get_inheritable(fd),
        same_0_1=os.fstat(0).st_ino == os.fstat(1).st_ino)
    after = s.recv(16)
    try:
        s.send(b"x")
        write = "ok"
    except OSError as exc:
        write = errno.errorcode.get(exc.errno, str(exc.errno))
    say(after=after.decode(), write=write)
    ch.recv(1)
''')


def fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def pidfd_of(test: unittest.TestCase, pid: int | None = None) -> int:
    """A process descriptor of ``pid`` (the test's own process by default), closed when the test ends.

    Where no process descriptor exists (not Linux) an open descriptor of the null device stands in:
    the calls that get it are refused for the platform before they look at it.
    """
    if getattr(os, "pidfd_open", None) is None:
        fd = os.open(os.devnull, os.O_RDONLY)
    else:
        fd = os.pidfd_open(os.getpid() if pid is None else pid)
    test.addCleanup(os.close, fd)
    return fd


class EchoServer:
    """Loopback only. ``greet`` sends b'hello' 0.2 s after accepting, before any echo."""

    def __init__(self, test: unittest.TestCase, greet: bool = False) -> None:
        self.greet = greet
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.address = self.listener.getsockname()
        self.eofs = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        test.addCleanup(self.stop)

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    def _echo(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(15)
            try:
                if self.greet:
                    time.sleep(0.2)
                    conn.sendall(b"hello")
                while True:
                    data = conn.recv(4096)
                    if not data:
                        self.eofs.append(True)
                        return
                    conn.sendall(data)
            except OSError:
                return

    def connect(self) -> socket.socket:
        return socket.create_connection(self.address, timeout=5)

    def stop(self) -> None:
        self.listener.close()


def read_line(sock: socket.socket, timeout: float = 10.0) -> dict:
    sock.settimeout(timeout)
    buf = b""
    while not buf.endswith(b"\n"):
        part = sock.recv(1)
        if not part:
            raise AssertionError(f"channel ended before a line: {buf!r}")
        buf += part
    return json.loads(buf)


def raw_send(channel: socket.socket, fds: list[int], data: bytes = b"m") -> None:
    channel.sendmsg([data], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))])


class PlatformTests(unittest.TestCase):
    """(P7) Where the hand-over is not shown it is refused with a named reason."""

    def test_linux_with_the_three_names_may_hand_over(self) -> None:
        stand_in = mock.Mock(spec=["SCM_RIGHTS", "MSG_CMSG_CLOEXEC", "SO_PEERCRED"])
        self.assertTrue(handover_shown("linux", stand_in))
        require_handover_platform("linux", stand_in)

    def test_macos_and_native_windows_are_refused_in_plain_words(self) -> None:
        stand_in = mock.Mock(spec=["SCM_RIGHTS", "MSG_CMSG_CLOEXEC", "SO_PEERCRED"])
        for platform in ("darwin", "win32", "cygwin", "freebsd14"):
            with self.subTest(platform=platform):
                self.assertFalse(handover_shown(platform, stand_in))
                with self.assertRaises(Refused) as caught:
                    require_handover_platform(platform, stand_in)
                self.assertEqual(PLATFORM_CODE, caught.exception.code)
                self.assertEqual("handing a connection to a plugin host is not shown on this platform",
                                 caught.exception.detail)

    def test_linux_without_scm_rights_or_cloexec_receipt_is_refused(self) -> None:
        for missing in ("SCM_RIGHTS", "MSG_CMSG_CLOEXEC", "SO_PEERCRED"):
            with self.subTest(missing=missing):
                names = [n for n in ("SCM_RIGHTS", "MSG_CMSG_CLOEXEC", "SO_PEERCRED") if n != missing]
                stand_in = mock.Mock(spec=names)
                self.assertFalse(handover_shown("linux", stand_in))
                with self.assertRaises(Refused) as caught:
                    require_handover_platform("linux", stand_in)
                self.assertEqual((PLATFORM_CODE, NOT_SHOWN), (caught.exception.code, caught.exception.detail))

    def test_the_code_is_the_existing_platform_code(self) -> None:
        self.assertEqual("platform_unsupported", PLATFORM_CODE)
        self.assertEqual("handover_refused", REFUSED_CODE)

    @unittest.skipIf(os.name == "nt", "the stand-in channel is a POSIX socket pair; the words are tested above")
    def test_send_and_receive_refuse_on_a_platform_where_it_is_not_shown(self) -> None:
        a, b = socket.socketpair()
        c, d = socket.socketpair()
        for s in (a, b, d):
            self.addCleanup(s.close)
        b.settimeout(1.0)  # a receive that is not refused would wait for a message that never comes
        with mock.patch.object(handover.sys, "platform", "darwin"):
            with self.assertRaises(Refused) as sent:
                HandoverTable().hand_over("host-1", a, c, PAYLOAD, host_pidfd=pidfd_of(self))
            with self.assertRaises(Refused) as received:
                receive_handover(b, 4096, timeout=1.0)
        self.assertEqual((PLATFORM_CODE, NOT_SHOWN), (sent.exception.code, sent.exception.detail))
        self.assertEqual((PLATFORM_CODE, NOT_SHOWN), (received.exception.code, received.exception.detail))
        self.assertEqual(-1, c.fileno())


@unittest.skipUnless(LINUX, "the hand-over is shown on Linux only")
class ReceiveTests(unittest.TestCase):
    """(P1) recvmsg with MSG_CMSG_CLOEXEC; one descriptor per message; nothing left open on a refusal."""

    def setUp(self) -> None:
        self.a, self.b = socket.socketpair()
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)
        self.b.settimeout(5)

    def test_a_received_socket_is_close_on_exec_and_a_child_does_not_see_it(self) -> None:
        x, y = socket.socketpair()
        self.addCleanup(y.close)
        raw_send(self.a, [x.fileno()])
        x.close()
        data, got = receive_handover(self.b, 4096, timeout=2.0)
        self.addCleanup(got.close)
        self.assertEqual(b"m", data)
        fd = got.fileno()
        self.assertTrue(fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC)
        self.assertFalse(os.get_inheritable(fd))
        probe = f"import os\ntry:\n os.fstat({fd}); print('open')\nexcept OSError: print('closed')"
        child = subprocess.run([sys.executable, "-c", probe], close_fds=False, capture_output=True, text=True,
                               timeout=30)
        self.assertEqual("closed", child.stdout.strip())
        y.sendall(b"ok")
        self.assertEqual(b"ok", got.recv(2))

    def test_the_receive_never_calls_the_standard_librarys_recv_fds(self) -> None:
        x, y = socket.socketpair()
        self.addCleanup(y.close)
        raw_send(self.a, [x.fileno()])
        x.close()
        with mock.patch.object(socket, "recv_fds", side_effect=AssertionError("recv_fds called")):
            _, got = receive_handover(self.b, 4096, timeout=2.0)
        got.close()

    def test_two_descriptors_are_refused_and_both_closed(self) -> None:
        x, y = socket.socketpair()
        self.addCleanup(x.close)
        self.addCleanup(y.close)
        raw_send(self.a, [x.fileno(), y.fileno()])
        before = fd_count()
        with self.assertRaises(Refused) as caught:
            receive_handover(self.b, 4096, timeout=2.0)
        self.assertEqual((REFUSED_CODE, "descriptor_count"), (caught.exception.code, caught.exception.detail))
        self.assertEqual(before, fd_count())

    def test_a_truncated_control_message_is_refused_and_what_came_is_closed(self) -> None:
        pairs = [socket.socketpair() for _ in range(2)]
        fds = [pairs[0][0].fileno(), pairs[0][1].fileno(), pairs[1][0].fileno(), pairs[1][1].fileno()]
        for pair in pairs:
            for s in pair:
                self.addCleanup(s.close)
        raw_send(self.a, fds)
        before = fd_count()
        with self.assertRaises(Refused) as caught:
            receive_handover(self.b, 4096, timeout=2.0)
        self.assertEqual((REFUSED_CODE, "control_truncated"), (caught.exception.code, caught.exception.detail))
        self.assertEqual(before, fd_count())

    def test_a_descriptor_that_is_not_a_socket_is_refused_and_closed(self) -> None:
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, w)
        raw_send(self.a, [r])
        before = fd_count()
        with self.assertRaises(Refused) as caught:
            receive_handover(self.b, 4096, timeout=2.0)
        self.assertEqual((REFUSED_CODE, "not_a_socket"), (caught.exception.code, caught.exception.detail))
        self.assertEqual(before, fd_count())

    def test_a_message_without_a_descriptor_answers_none(self) -> None:
        self.a.sendall(b"plain")
        self.assertEqual((b"plain", None), receive_handover(self.b, 4096, timeout=1.0))

    def test_the_callers_mistakes_raise(self) -> None:
        for bad in (0, -1, True, "4096"):
            with self.subTest(bufsize=bad), self.assertRaises(TypeError):
                receive_handover(self.b, bad, timeout=1.0)
        for bad in (-1, True, "1"):
            with self.subTest(timeout=bad), self.assertRaises(TypeError):
                receive_handover(self.b, 4096, timeout=bad)


@unittest.skipUnless(LINUX, "the hand-over is shown on Linux only")
class InProcessHandoverTests(unittest.TestCase):
    """The sending side with an in-process receiver over a socket pair (P2, P3, P4, P5)."""

    def setUp(self) -> None:
        self.channel, self.host_end = socket.socketpair()
        self.addCleanup(self.channel.close)
        self.addCleanup(self.host_end.close)
        self.host_end.settimeout(5)
        self.table = HandoverTable()
        self.addCleanup(self.table.cut_host, "host-1")

    def hand(self, sock: socket.socket, **kw):
        return self.table.hand_over("host-1", self.channel, sock, PAYLOAD, host_pidfd=pidfd_of(self), **kw)

    # (P2, condition 3)
    def test_the_receiver_sets_its_own_blocking_mode_whatever_the_sender_had(self) -> None:
        server = EchoServer(self, greet=True)
        client = server.connect()
        client.settimeout(5.0)  # the shared open connection is now non-blocking
        self.assertEqual(HANDED, self.hand(client).outcome)
        data, got = receive_handover(self.host_end, 4096, timeout=None)
        self.addCleanup(got.close)
        self.assertEqual(PAYLOAD, data)
        self.assertIsNone(got.gettimeout())
        self.assertEqual(b"hello", got.recv(5))  # sent 0.2 s after accept: a blocking read waits for it

    def test_the_receiver_takes_the_timeout_it_is_given(self) -> None:
        x, y = socket.socketpair()
        self.addCleanup(y.close)
        self.assertEqual(HANDED, self.hand(x).outcome)
        _, got = receive_handover(self.host_end, 4096, timeout=2.5)
        self.addCleanup(got.close)
        self.assertEqual(2.5, got.gettimeout())

    # (P3, condition 4)
    def test_a_cut_ends_the_receivers_blocked_read_with_eof_and_its_write_with_epipe(self) -> None:
        server = EchoServer(self)
        client = server.connect()
        result = self.hand(client)
        self.assertEqual(HANDED, result.outcome)
        self.assertEqual(-1, client.fileno())  # the table keeps the core's copy
        self.assertEqual((result.handle,), self.table.handed("host-1"))
        _, got = receive_handover(self.host_end, 4096, timeout=3.0)
        self.addCleanup(got.close)
        got.sendall(b"ping")
        self.assertEqual(b"ping", got.recv(4))
        seen = {}

        def blocked_read() -> None:
            try:
                seen["data"] = got.recv(16)
            except OSError as exc:
                seen["error"] = exc
            seen["at"] = time.monotonic()

        reader = threading.Thread(target=blocked_read)
        reader.start()
        time.sleep(0.2)
        cut_at = time.monotonic()
        self.assertTrue(self.table.cut("host-1", result.handle))
        reader.join(5)
        self.assertEqual(b"", seen.get("data"), seen)
        self.assertLess(seen["at"] - cut_at, 1.0)
        with self.assertRaises(BrokenPipeError):
            got.send(b"x")
        self.assertEqual((), self.table.handed("host-1"))
        self.assertFalse(self.table.cut("host-1", result.handle))

    def test_cut_host_cuts_every_socket_of_that_host_only(self) -> None:
        other = HandoverTable()
        received = []
        handles = []
        for _ in range(3):
            x, y = socket.socketpair()
            self.addCleanup(y.close)
            handles.append(self.hand(x).handle)
            _, got = receive_handover(self.host_end, 4096, timeout=3.0)
            self.addCleanup(got.close)
            received.append(got)
        self.assertEqual(tuple(sorted(handles)), self.table.handed("host-1"))
        self.assertEqual(0, self.table.cut_host("host-2"))
        self.assertEqual(3, self.table.cut_host("host-1"))
        for got in received:
            self.assertEqual(b"", got.recv(1))
        self.assertEqual((), self.table.handed("host-1"))
        self.assertEqual((), other.handed("host-1"))

    def test_close_alone_does_not_end_the_receivers_use(self) -> None:
        server = EchoServer(self)
        client = server.connect()
        raw_send(self.channel, [client.fileno()])
        client.close()  # the sender's only copy, closed, not shut down
        _, got = receive_handover(self.host_end, 4096, timeout=3.0)
        self.addCleanup(got.close)
        got.sendall(b"still")
        self.assertEqual(b"still", got.recv(5))

    # (P4, condition 6)
    def test_a_full_channel_answers_host_slow_within_the_cap_and_keeps_nothing(self) -> None:
        handed = 0
        slow = None
        for _ in range(2000):
            x, y = socket.socketpair()
            self.addCleanup(x.close)
            self.addCleanup(y.close)
            started = time.monotonic()
            result = self.hand(x, send_timeout_s=0.2)
            took = time.monotonic() - started
            if result.outcome == HOST_SLOW:
                slow = (x, took, result)
                break
            handed += 1
        self.assertIsNotNone(slow, "the channel never filled")
        x, took, result = slow
        self.assertGreater(handed, 10)
        self.assertIsNone(result.handle)
        self.assertGreaterEqual(took, 0.19)
        self.assertLess(took, 1.0)
        self.assertNotEqual(-1, x.fileno())  # not handed: still the caller's
        self.assertEqual(handed, len(self.table.handed("host-1")))

    def test_the_send_cap_is_an_argument_with_the_first_value_half_a_second(self) -> None:
        self.assertEqual(0.5, HANDOVER_SEND_TIMEOUT_S)
        default = inspect.signature(HandoverTable.hand_over).parameters["send_timeout_s"].default
        self.assertEqual(0.5, default)

    # (P5, condition 7)
    def test_a_host_whose_channel_has_ended_is_refused_at_once_and_the_connection_closed(self) -> None:
        server = EchoServer(self)
        client = server.connect()
        self.host_end.close()
        started = time.monotonic()
        with self.assertRaises(Refused) as caught:
            self.hand(client)
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual((REFUSED_CODE, "host_gone"), (caught.exception.code, caught.exception.detail))
        self.assertEqual(-1, client.fileno())
        self.assertEqual((), self.table.handed("host-1"))

    def test_a_message_cut_off_after_its_descriptor_went_is_refused_and_the_connection_cut(self) -> None:
        class HalfChannel(socket.socket):
            def sendmsg(self, *args, **kwargs):
                return 1

            def send(self, *args, **kwargs):
                raise BlockingIOError(11, "full")

        half = HalfChannel(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(half.close)
        server = EchoServer(self)
        client = server.connect()
        with self.assertRaises(Refused) as caught:
            self.table.hand_over("host-1", half, client, PAYLOAD, host_pidfd=pidfd_of(self), send_timeout_s=0.1)
        self.assertEqual((REFUSED_CODE, "channel_broken"), (caught.exception.code, caught.exception.detail))
        self.assertEqual(-1, client.fileno())
        self.assertEqual((), self.table.handed("host-1"))

    def test_the_callers_mistakes_raise_and_close_nothing(self) -> None:
        x, y = socket.socketpair()
        self.addCleanup(x.close)
        self.addCleanup(y.close)
        cases = [dict(host_id=""), dict(payload=b""), dict(payload="text"), dict(host_pidfd=-1),
                 dict(host_pidfd=True), dict(send_timeout_s=-1), dict(send_timeout_s="1")]
        own = pidfd_of(self)
        for case in cases:
            args = dict(host_id="host-1", channel=self.channel, sock=x, payload=PAYLOAD, host_pidfd=own)
            args.update(case)
            with self.subTest(case=case), self.assertRaises(TypeError):
                self.table.hand_over(**args)
        self.assertNotEqual(-1, x.fileno())


@unittest.skipUnless(LINUX, "the hand-over is shown on Linux only")
class UserNamespaceTests(unittest.TestCase):
    """(P6, condition 5) The host's user namespace is compared with the core's own."""

    def test_so_peercred_on_a_socket_pair_names_the_core_not_the_host(self) -> None:
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"], stdin=b.fileno())
        b.close()
        self.addCleanup(child.wait, 10)
        cred = a.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        pid = int.from_bytes(cred[:4], sys.byteorder, signed=True)
        a.sendall(b"x")
        self.assertEqual(os.getpid(), pid)
        self.assertNotEqual(child.pid, pid)

    def test_a_host_that_has_ended_is_refused_before_any_send(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        host_pidfd = pidfd_of(self, child.pid)
        child.wait(30)
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        x, y = socket.socketpair()
        self.addCleanup(y.close)
        with self.assertRaises(Refused) as caught:
            HandoverTable().hand_over("host-1", a, x, PAYLOAD, host_pidfd=host_pidfd)
        self.assertEqual((REFUSED_CODE, "host_gone"), (caught.exception.code, caught.exception.detail))
        self.assertEqual(-1, x.fileno())
        b.setblocking(False)
        with self.assertRaises(BlockingIOError):
            b.recv(1)  # nothing entered the channel

    def test_an_unreadable_namespace_is_refused(self) -> None:
        a, b = socket.socketpair()
        x, y = socket.socketpair()
        for s in (a, b, y):
            self.addCleanup(s.close)
        with mock.patch.object(handover.os, "stat", side_effect=PermissionError(13, "denied")):
            with self.assertRaises(Refused) as caught:
                HandoverTable().hand_over("host-1", a, x, PAYLOAD, host_pidfd=pidfd_of(self))
        self.assertEqual((REFUSED_CODE, "user_namespace_unreadable"),
                         (caught.exception.code, caught.exception.detail))
        self.assertEqual(-1, x.fileno())


class HostRun:
    """A host process with the test's host script; its fd 0 and fd 1 are one end of a socket pair."""

    def __init__(self, test: unittest.TestCase, unit_properties: list[str] | None = None) -> None:
        folder = tempfile.mkdtemp()
        test.addCleanup(shutil.rmtree, folder, True)
        script = os.path.join(folder, "host.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(HOST_SCRIPT)
        self.channel, end = socket.socketpair()
        test.addCleanup(self.channel.close)
        argv = [sys.executable, "-B", script, CORE]
        self.unit = None
        if unit_properties is not None:
            HostRun.count += 1
            self.unit = f"orderstest-handover-{os.getpid()}-{HostRun.count}"
            argv = (["systemd-run", "--user", "--quiet", "--service-type=exec", "--expand-environment=no",
                     f"--unit={self.unit}"] + [f"--property={p}" for p in unit_properties] + ["--pipe", "--"]
                    + argv)
            test.addCleanup(self.stop_and_show_gone, test)
        self.proc = subprocess.Popen(argv, stdin=end.fileno(), stdout=end.fileno())
        end.close()
        test.addCleanup(self._reap)
        self.pid = self._main_pid() if self.unit else self.proc.pid
        self.pidfd = pidfd_of(test, self.pid)

    count = 0

    def _main_pid(self) -> int:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            out = subprocess.run(["systemctl", "--user", "show", "-p", "MainPID", "--value", self.unit],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
            if out.isdigit() and int(out) > 0:
                return int(out)
            time.sleep(0.02)
        raise AssertionError(f"no main pid for {self.unit}")

    def _reap(self) -> None:
        self.channel.close()  # the host waits for the end of its channel, then exits
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(10)

    def stop_and_show_gone(self, test: unittest.TestCase) -> None:
        subprocess.run(["systemctl", "--user", "stop", self.unit], capture_output=True, timeout=30)
        subprocess.run(["systemctl", "--user", "reset-failed", self.unit], capture_output=True, timeout=30)
        listed = subprocess.run(["systemctl", "--user", "list-units", self.unit, "--all", "--no-legend"],
                                capture_output=True, text=True, timeout=30).stdout.strip()
        test.assertEqual("", listed, f"unit {self.unit} is not gone")


def user_manager_answers() -> bool:
    if not LINUX or shutil.which("systemd-run") is None or shutil.which("systemctl") is None:
        return False
    try:
        return subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True,
                              timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@unittest.skipUnless(LINUX, "the hand-over is shown on Linux only")
class HostProcessTests(unittest.TestCase):
    """The whole path to a real host process: a plain child (d2) and a unit of the user manager (d1)."""

    def run_host(self, host: HostRun) -> None:
        server = EchoServer(self)
        client = server.connect()
        client.settimeout(5.0)
        table = HandoverTable()
        self.addCleanup(table.cut_host, "host-x")
        result = table.hand_over("host-x", host.channel, client, PAYLOAD, host_pidfd=host.pidfd)
        self.assertEqual(HANDED, result.outcome)
        first = read_line(host.channel)
        self.assertEqual({"got": PAYLOAD.decode(), "echo": "ping-1", "child": "closed", "cloexec": True,
                          "inheritable": False, "same_0_1": True}, first)
        self.assertTrue(table.cut("host-x", result.handle))
        self.assertEqual({"after": "", "write": "EPIPE"}, read_line(host.channel))

    def test_a_plain_child_host_receives_uses_and_loses_the_connection(self) -> None:
        self.run_host(HostRun(self))

    @unittest.skipUnless(user_manager_answers(), "no user service manager answers here")
    def test_a_unit_host_receives_uses_and_loses_the_connection(self) -> None:
        self.run_host(HostRun(self, []))

    @unittest.skipUnless(user_manager_answers(), "no user service manager answers here")
    def test_a_unit_host_in_another_user_namespace_is_refused(self) -> None:
        host = HostRun(self, ["PrivateUsers=yes"])
        server = EchoServer(self)
        client = server.connect()
        table = HandoverTable()
        with self.assertRaises(Refused) as caught:
            table.hand_over("host-x", host.channel, client, PAYLOAD, host_pidfd=host.pidfd)
        self.assertEqual((REFUSED_CODE, "other_user_namespace"), (caught.exception.code, caught.exception.detail))
        self.assertEqual(-1, client.fileno())
        self.assertEqual((), table.handed("host-x"))
        host.channel.shutdown(socket.SHUT_WR)
        self.assertEqual({"got": None}, read_line(host.channel))


def nothing_in(host_end: socket.socket) -> bool:
    host_end.setblocking(False)
    try:
        host_end.recv(1)
    except BlockingIOError:
        return True
    finally:
        host_end.setblocking(True)
    return False


@unittest.skipUnless(LINUX, "the hand-over is shown on Linux only")
class ConnectedStreamTests(unittest.TestCase):
    """Only a connected stream socket goes through; any other kind is refused before anything is sent."""

    def test_only_a_connected_stream_socket_is_handed(self) -> None:
        channel, host_end = socket.socketpair()
        self.addCleanup(channel.close)
        self.addCleanup(host_end.close)
        table = HandoverTable()
        self.addCleanup(table.cut_host, "host-1")
        kinds = {}
        kinds["tcp listening"] = socket.create_server(("127.0.0.1", 0))
        kinds["tcp never connected"] = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        kinds["udp"] = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        target = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(target.close)
        target.bind(("127.0.0.1", 0))
        kinds["udp connected"] = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        kinds["udp connected"].connect(target.getsockname())
        kinds["unix datagram"], other = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(other.close)
        kinds["unix listening"] = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        kinds["unix listening"].bind("")  # an abstract name the kernel picks
        kinds["unix listening"].listen()
        try:
            kinds["netlink"] = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0)
        except (AttributeError, OSError):
            pass  # no netlink here: the other kinds still show the rule
        for name, sock in kinds.items():
            self.addCleanup(sock.close)
            with self.subTest(kind=name):
                with self.assertRaises(ValueError) as caught:
                    table.hand_over("host-1", channel, sock, PAYLOAD, host_pidfd=pidfd_of(self))
                self.assertIn("connected stream socket", str(caught.exception))
                self.assertNotEqual(-1, sock.fileno())  # the caller's mistake: still the caller's
                self.assertTrue(nothing_in(host_end), "something was sent")
                self.assertEqual((), table.handed("host-1"))
        server = EchoServer(self)
        unix_a, unix_b = socket.socketpair()
        self.addCleanup(unix_b.close)
        for name, sock in (("tcp connected", server.connect()), ("unix stream connected", unix_a)):
            self.addCleanup(sock.close)
            with self.subTest(kind=name):
                self.assertEqual(HANDED, table.hand_over("host-1", channel, sock, PAYLOAD,
                                                         host_pidfd=pidfd_of(self)).outcome)
                _, got = receive_handover(host_end, 4096, timeout=2.0)
                self.addCleanup(got.close)
                self.assertEqual(socket.SOCK_STREAM, got.type)


@unittest.skipUnless(LINUX, "the hand-over is shown on Linux only")
class ChannelTimeoutTests(unittest.TestCase):
    """The cap holds whatever timeout the channel object carries: a slow host is HOST_SLOW, never TimeoutError."""

    def test_a_channel_with_a_timeout_of_its_own_still_answers_host_slow_within_the_cap(self) -> None:
        channel, host_end = socket.socketpair()
        self.addCleanup(channel.close)
        self.addCleanup(host_end.close)
        channel.settimeout(2.0)
        blocking = os.get_blocking(channel.fileno())
        table = HandoverTable()
        self.addCleanup(table.cut_host, "host-1")
        handed = 0
        slow = None
        for _ in range(2000):
            x, y = socket.socketpair()
            self.addCleanup(x.close)
            self.addCleanup(y.close)
            started = time.monotonic()
            result = table.hand_over("host-1", channel, x, PAYLOAD, host_pidfd=pidfd_of(self), send_timeout_s=0.1)
            if result.outcome == HOST_SLOW:
                slow = (x, time.monotonic() - started)
                break
            handed += 1
        self.assertIsNotNone(slow, "the channel never filled")
        x, took = slow
        self.assertGreater(handed, 10)
        self.assertGreaterEqual(took, 0.09)
        self.assertLess(took, 0.6)
        self.assertNotEqual(-1, x.fileno())
        self.assertEqual(handed, len(table.handed("host-1")))
        x, _y = socket.socketpair()  # the first value of the cap, against a channel that would wait 2 s
        self.addCleanup(x.close)
        self.addCleanup(_y.close)
        started = time.monotonic()
        self.assertEqual(HOST_SLOW, table.hand_over("host-1", channel, x, PAYLOAD, host_pidfd=pidfd_of(self)).outcome)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertNotEqual(-1, x.fileno())
        self.assertEqual(2.0, channel.gettimeout())  # the channel's own timeout and mode are untouched
        self.assertEqual(blocking, os.get_blocking(channel.fileno()))


RACE_SCRIPT = textwrap.dedent('''\
    import os, socket, subprocess, sys
    sys.path.insert(0, sys.argv[1])
    from recursion_core.plugin_security import Refused
    from recursion_core.plugin_security.handover import HandoverTable
    HOST = chr(10).join(["import os, sys", "if os.fork():", " os._exit(0)", "os.unshare(os.CLONE_NEWUSER)",
                         "os.write(1, b'g')", "sys.stdin.buffer.read()", ""])
    a, b = socket.socketpair()
    host = subprocess.Popen([sys.executable, "-B", "-c", HOST], stdin=b.fileno(), stdout=b.fileno())
    host_pidfd = os.pidfd_open(host.pid)
    b.close()
    number = host.pid
    host.wait()
    a.settimeout(10)
    if a.recv(1) != b"g":
        print("setup: the host's child did not move")
        sys.exit(0)
    a.settimeout(None)
    with open("/proc/sys/kernel/ns_last_pid", "w") as handle:
        handle.write(str(number - 1))
    innocent = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    if innocent.pid != number:
        print("setup: the number went to another process", innocent.pid, number)
    else:
        x, y = socket.socketpair()
        try:
            print("answer", HandoverTable().hand_over("host-x", a, x, b"m", host_pidfd=host_pidfd).outcome)
        except Refused as exc:
            print("answer", exc.detail)
        except Exception as exc:
            print("answer", type(exc).__name__)
    innocent.stdin.close()
    innocent.wait()
    a.close()
    while True:
        try:
            os.wait()
        except ChildProcessError:
            break
''')


@unittest.skipUnless(LINUX, "the hand-over is shown on Linux only")
class ProcessDescriptorTests(unittest.TestCase):
    """The host is held by a process descriptor: an ended host's process id can never name another process."""

    def test_an_ended_hosts_number_given_to_another_process_is_host_gone(self) -> None:
        # Run inside a user and pid namespace of the test's own, where the next process id may be chosen;
        # every process of it ends with the namespace.
        if shutil.which("unshare") is None:
            self.skipTest("no unshare here")
        run = subprocess.run(["unshare", "--user", "--map-root-user", "--pid", "--fork", "--mount-proc",
                              sys.executable, "-B", "-c", RACE_SCRIPT, CORE],
                             capture_output=True, text=True, timeout=120)
        out = run.stdout.strip()
        if run.returncode != 0 and not out:
            self.skipTest("a namespace of the test's own may not be made here: " + run.stderr.strip()[-200:])
        if out.startswith("setup:"):
            self.skipTest(out)
        self.assertEqual("answer host_gone", out, run.stderr[-400:])

    def test_a_host_that_has_ended_and_is_not_yet_reaped_is_refused(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        self.addCleanup(child.wait, 30)
        host_pidfd = pidfd_of(self, child.pid)
        poller = select.poll()
        poller.register(host_pidfd, select.POLLIN)
        self.assertTrue(poller.poll(30000), "the child did not end")  # ended, and nobody has reaped it
        channel, host_end = socket.socketpair()
        self.addCleanup(channel.close)
        self.addCleanup(host_end.close)
        x, y = socket.socketpair()
        self.addCleanup(y.close)
        with self.assertRaises(Refused) as caught:
            HandoverTable().hand_over("host-1", channel, x, PAYLOAD, host_pidfd=host_pidfd)
        self.assertEqual((REFUSED_CODE, "host_gone"), (caught.exception.code, caught.exception.detail))
        self.assertEqual(-1, x.fileno())
        self.assertTrue(nothing_in(host_end))

    def test_where_no_process_descriptor_is_given_the_hand_over_is_refused(self) -> None:
        stand_ins = {
            "a Python without os.pidfd_open": mock.patch.object(handover.os, "pidfd_open", None),
            "a kernel whose descriptor names no process": mock.patch.object(
                handover, "_fdinfo", return_value="pos:\t0\nflags:\t02000002\nmnt_id:\t15\nino:\t1\n"),
        }
        for name, stand_in in stand_ins.items():
            with self.subTest(case=name):
                channel, host_end = socket.socketpair()
                self.addCleanup(channel.close)
                self.addCleanup(host_end.close)
                x, y = socket.socketpair()
                self.addCleanup(y.close)
                own = pidfd_of(self)
                with stand_in, self.assertRaises(Refused) as caught:
                    HandoverTable().hand_over("host-1", channel, x, PAYLOAD, host_pidfd=own)
                self.assertEqual((PLATFORM_CODE, NOT_SHOWN), (caught.exception.code, caught.exception.detail))
                self.assertEqual(-1, x.fileno())
                self.assertTrue(nothing_in(host_end))

    def test_a_descriptor_that_is_not_open_is_the_callers_mistake(self) -> None:
        channel, host_end = socket.socketpair()
        x, y = socket.socketpair()
        for s in (channel, host_end, x, y):
            self.addCleanup(s.close)
        fd = 1 << 20  # no descriptor of this number is open
        with self.assertRaises(ValueError):
            HandoverTable().hand_over("host-1", channel, x, PAYLOAD, host_pidfd=fd)
        self.assertNotEqual(-1, x.fileno())


@unittest.skipUnless(LINUX, "the hand-over is shown on Linux only")
class HeldDescriptorTests(unittest.TestCase):
    """The socket's descriptor is taken once: a close by another thread during the wait sends no other file."""

    def test_a_close_during_the_wait_cannot_send_another_file_of_the_core(self) -> None:
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        secret = os.path.join(folder, "core-own-file.txt")
        with open(secret, "w", encoding="utf-8") as handle:
            handle.write("a file of the core, never meant for a plugin")
        a, host_end = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(host_end.close)
        host_end.settimeout(5)
        x, y = socket.socketpair()
        self.addCleanup(y.close)
        taken = []

        class Swapping(socket.socket):
            """While the first send waits, the caller's other thread closes ``x`` and its number goes to a file."""

            def sendmsg(self, buffers, ancdata=(), flags=0, *rest):
                if not taken:
                    number = x.fileno()
                    x.close()
                    fd = os.open(secret, os.O_RDONLY)
                    if fd != number:
                        os.dup2(fd, number)
                        os.close(fd)
                    taken.append(number)
                    raise BlockingIOError(11, "full")
                return super().sendmsg(buffers, ancdata, flags, *rest)

        channel = Swapping(fileno=os.dup(a.fileno()))
        self.addCleanup(channel.close)
        table = HandoverTable()

        def cut_quietly() -> None:
            try:
                table.cut_host("host-1")
            except (OSError, ValueError):
                pass

        self.addCleanup(cut_quietly)
        result = table.hand_over("host-1", channel, x, PAYLOAD, host_pidfd=pidfd_of(self))
        self.addCleanup(os.close, taken[0])
        self.assertEqual(HANDED, result.outcome)
        _, anc, _, _ = host_end.recvmsg(4096, socket.CMSG_SPACE(4), socket.MSG_CMSG_CLOEXEC)
        fds = array.array("i")
        fds.frombytes(anc[0][2][:4])
        self.addCleanup(os.close, fds[0])
        self.assertTrue(stat.S_ISSOCK(os.fstat(fds[0]).st_mode), "the host received a file of the core")
        got = socket.socket(fileno=os.dup(fds[0]))
        self.addCleanup(got.close)
        got.settimeout(2)
        y.sendall(b"ok")
        self.assertEqual(b"ok", got.recv(2))  # the very connection the caller gave
        self.assertEqual("a file of the core, never meant for a plugin", os.pread(taken[0], 80, 0).decode())


class Recording(socket.socket):
    """A socket that notes the calls made on it and the flags of every recvmsg."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls = []
        self.recv_flags = []

    def recvmsg(self, bufsize, ancbufsize=0, flags=0):
        self.calls.append("recvmsg")
        self.recv_flags.append(flags)
        return super().recvmsg(bufsize, ancbufsize, flags)

    def sendmsg(self, *args, **kwargs):
        self.calls.append("sendmsg")
        return super().sendmsg(*args, **kwargs)

    def send(self, *args, **kwargs):
        self.calls.append("send")
        return super().send(*args, **kwargs)


@unittest.skipUnless(LINUX, "the hand-over is shown on Linux only")
class PromiseTests(unittest.TestCase):
    """Promises of the module that each have a test of their own: each goes red when its line is cut."""

    def pair(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        return a, b

    def tcp(self):
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        client = socket.create_connection(listener.getsockname(), timeout=5)
        self.addCleanup(client.close)
        conn, _ = listener.accept()
        self.addCleanup(conn.close)
        return client, conn

    def test_close_on_exec_is_asked_for_in_the_receiving_call_itself(self) -> None:
        a, b = self.pair()
        rec = Recording(fileno=os.dup(b.fileno()))
        self.addCleanup(rec.close)
        rec.settimeout(5)
        x, _y = self.pair()
        a.sendmsg([b"m"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [x.fileno()]))])
        with mock.patch.object(handover.os, "set_inheritable",
                               side_effect=AssertionError("the flag was set after the receipt")):
            _, got = receive_handover(rec, 4096, timeout=1.0)
        self.addCleanup(got.close)
        self.assertEqual(1, len(rec.recv_flags))
        self.assertTrue(rec.recv_flags[0] & socket.MSG_CMSG_CLOEXEC)

    def test_a_cut_ends_reads_from_a_peer_that_keeps_sending(self) -> None:
        client, conn = self.tcp()
        stop = threading.Event()
        self.addCleanup(stop.set)

        def chatter() -> None:  # a far end that ignores the end of our side and goes on sending
            while not stop.is_set():
                try:
                    conn.sendall(b"tick")
                except OSError:
                    return
                time.sleep(0.02)

        threading.Thread(target=chatter, daemon=True).start()
        channel, host_end = self.pair()
        host_end.settimeout(5)
        table = HandoverTable()
        result = table.hand_over("host-1", channel, client, PAYLOAD, host_pidfd=pidfd_of(self))
        _, got = receive_handover(host_end, 4096, timeout=2.0)
        self.addCleanup(got.close)
        self.assertEqual(b"tick", got.recv(4))
        self.assertTrue(table.cut("host-1", result.handle))
        deadline = time.monotonic() + 1.0
        while True:  # what was already queued may still be read; then it must end
            try:
                data = got.recv(65536)
            except (ConnectionResetError, BrokenPipeError):
                break
            if not data:
                break
            self.assertLess(time.monotonic(), deadline, "the host still reads from the far end after the cut")
        time.sleep(0.2)
        try:
            self.assertEqual(b"", got.recv(16))
        except (ConnectionResetError, BrokenPipeError):
            pass
        with self.assertRaises((BrokenPipeError, ConnectionResetError)):
            got.send(b"x")

    def test_the_cap_given_is_the_cap_used(self) -> None:
        channel, _host_end = self.pair()
        table = HandoverTable()
        self.addCleanup(table.cut_host, "host-1")
        took = None
        for _ in range(2000):
            x, y = socket.socketpair()
            self.addCleanup(x.close)
            self.addCleanup(y.close)
            started = time.monotonic()
            result = table.hand_over("host-1", channel, x, PAYLOAD, host_pidfd=pidfd_of(self), send_timeout_s=0.05)
            if result.outcome == HOST_SLOW:
                took = time.monotonic() - started
                break
        self.assertIsNotNone(took, "the channel never filled")
        self.assertGreaterEqual(took, 0.04)
        self.assertLess(took, 0.3)
        x, _y = self.pair()
        started = time.monotonic()
        result = table.hand_over("host-1", channel, x, PAYLOAD, host_pidfd=pidfd_of(self), send_timeout_s=0)
        self.assertEqual(HOST_SLOW, result.outcome)
        self.assertLess(time.monotonic() - started, 0.05)

    def test_a_really_half_sent_message_leaves_the_host_a_dead_connection(self) -> None:
        client, conn = self.tcp()
        channel, host_end = self.pair()
        host_end.settimeout(5)
        table = HandoverTable()
        big = b"x" * (1 << 20)
        with self.assertRaises(Refused) as caught:
            table.hand_over("host-1", channel, client, big, host_pidfd=pidfd_of(self), send_timeout_s=0.2)
        self.assertEqual((REFUSED_CODE, "channel_broken"), (caught.exception.code, caught.exception.detail))
        self.assertEqual(-1, client.fileno())
        self.assertEqual((), table.handed("host-1"))
        data, got = receive_handover(host_end, 65536, timeout=2.0)
        self.assertIsNotNone(got, "the descriptor went with the first bytes")
        self.addCleanup(got.close)
        self.assertTrue(data)
        self.assertEqual(b"", got.recv(16))  # the connection the host received is already cut
        with self.assertRaises((BrokenPipeError, ConnectionResetError)):
            got.send(b"x")
        conn.settimeout(2)
        self.assertEqual(b"", conn.recv(16))  # and the far end saw it end

    def test_the_sender_leaves_the_mode_of_the_shared_connection_alone(self) -> None:
        for sender_timeout, blocking in ((5.0, False), (None, True)):
            with self.subTest(sender_timeout=sender_timeout):
                x, _y = self.pair()
                x.settimeout(sender_timeout)
                channel, host_end = self.pair()
                host_end.settimeout(5)
                table = HandoverTable()
                result = table.hand_over("host-1", channel, x, PAYLOAD, host_pidfd=pidfd_of(self))
                self.assertEqual(HANDED, result.outcome)
                # take the descriptor by hand, without the receiving side's own mode line
                _, anc, _, _ = host_end.recvmsg(4096, socket.CMSG_SPACE(4), socket.MSG_CMSG_CLOEXEC)
                fds = array.array("i")
                fds.frombytes(anc[0][2][:4])
                self.addCleanup(os.close, fds[0])
                self.assertEqual(blocking, os.get_blocking(fds[0]))
                self.assertEqual((result.handle,), table.handed("host-1"))
                self.assertEqual(1, table.cut_host("host-1"))

    def test_a_plain_child_in_another_user_namespace_is_refused(self) -> None:
        code = ("import os, sys\n"
                "try:\n os.unshare(os.CLONE_NEWUSER)\n"
                "except (OSError, AttributeError) as exc:\n print('no', exc, flush=True); sys.exit(0)\n"
                "print('ok', flush=True)\nsys.stdin.read()\n")
        child = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 text=True)
        self.addCleanup(child.wait, 10)
        self.addCleanup(child.stdin.close)
        self.addCleanup(child.stdout.close)
        line = child.stdout.readline().strip()
        if line != "ok":
            self.skipTest("a plain process may not make a user namespace here: " + line)
        channel, host_end = self.pair()
        x, _y = self.pair()
        with self.assertRaises(Refused) as caught:
            HandoverTable().hand_over("host-1", channel, x, PAYLOAD, host_pidfd=pidfd_of(self, child.pid))
        self.assertEqual((REFUSED_CODE, "other_user_namespace"), (caught.exception.code, caught.exception.detail))
        self.assertEqual(-1, x.fileno())
        self.assertTrue(nothing_in(host_end))

    def test_a_differing_namespace_file_is_refused_with_stand_in_stats(self) -> None:
        real_stat = os.stat

        def fake(path, *args, **kwargs):
            st = real_stat("/proc/self/ns/user")
            if str(path) == "/proc/self/ns/user":
                return st
            return os.stat_result((st.st_mode, st.st_ino + 1, st.st_dev, st.st_nlink, st.st_uid, st.st_gid,
                                   st.st_size, st.st_atime, st.st_mtime, st.st_ctime))

        channel, _host_end = self.pair()
        x, _y = self.pair()
        own = pidfd_of(self)
        with mock.patch.object(handover.os, "stat", side_effect=fake):
            with self.assertRaises(Refused) as caught:
                HandoverTable().hand_over("host-1", channel, x, PAYLOAD, host_pidfd=own)
        self.assertEqual((REFUSED_CODE, "other_user_namespace"), (caught.exception.code, caught.exception.detail))
        self.assertEqual(-1, x.fileno())

    def test_nothing_is_attempted_where_the_hand_over_is_not_shown(self) -> None:
        a, b = self.pair()
        send_side = Recording(fileno=os.dup(a.fileno()))
        recv_side = Recording(fileno=os.dup(b.fileno()))
        self.addCleanup(send_side.close)
        self.addCleanup(recv_side.close)
        recv_side.settimeout(1.0)
        own = pidfd_of(self)
        for platform in ("darwin", "win32"):
            with self.subTest(platform=platform):
                x, _y = self.pair()
                with mock.patch.object(handover.sys, "platform", platform), \
                        mock.patch.object(handover.os, "stat", side_effect=AssertionError("/proc was read")), \
                        mock.patch.object(handover, "_fdinfo", side_effect=AssertionError("/proc was read")):
                    with self.assertRaises(Refused):
                        HandoverTable().hand_over("host-1", send_side, x, PAYLOAD, host_pidfd=own)
                    with self.assertRaises(Refused):
                        receive_handover(recv_side, 4096, timeout=1.0)
                self.assertEqual(-1, x.fileno())
        self.assertEqual([], send_side.calls)
        self.assertEqual([], recv_side.calls)
        self.assertTrue(nothing_in(b))

    def test_a_stop_waits_for_a_send_in_progress_and_cuts_what_it_handed(self) -> None:
        channel, host_end = self.pair()
        host_end.settimeout(5)
        table = HandoverTable()
        self.addCleanup(table.cut_host, "host-1")
        own = pidfd_of(self)
        n = 0
        for _ in range(2000):
            x, y = socket.socketpair()
            self.addCleanup(x.close)
            self.addCleanup(y.close)
            if table.hand_over("host-1", channel, x, PAYLOAD, host_pidfd=own, send_timeout_s=0).outcome == HOST_SLOW:
                break
            n += 1
        self.assertGreater(n, 10)
        x, y = self.pair()
        out = {}

        def sender() -> None:
            out["result"] = table.hand_over("host-1", channel, x, PAYLOAD, host_pidfd=own, send_timeout_s=4.0)

        def stopper() -> None:
            out["cut"] = table.cut_host("host-1")

        a_thread = threading.Thread(target=sender)
        a_thread.start()
        time.sleep(0.15)  # the send now waits on the full channel
        b_thread = threading.Thread(target=stopper)
        b_thread.start()
        time.sleep(0.15)
        for _ in range(n + 1):  # the host reads again: the waiting send goes through
            _, got = receive_handover(host_end, 4096, timeout=2.0)
            self.addCleanup(got.close)
        a_thread.join(6)
        b_thread.join(6)
        self.assertEqual(HANDED, out["result"].outcome)
        self.assertEqual((), table.handed("host-1"), "the stop missed the connection handed while it ran")
        self.assertEqual(n + 1, out["cut"])
        y.settimeout(2)
        self.assertEqual(b"", y.recv(1))

    def test_an_unexpected_channel_error_still_releases_the_connection(self) -> None:
        channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # never connected: ENOTCONN
        self.addCleanup(channel.close)
        x, y = self.pair()
        y.settimeout(2)
        with self.assertRaises(OSError):
            HandoverTable().hand_over("host-1", channel, x, PAYLOAD, host_pidfd=pidfd_of(self))
        self.assertEqual(-1, x.fileno())
        self.assertEqual(b"", y.recv(1))

    def test_a_host_that_takes_the_descriptor_then_shuts_its_channel_holds_a_dead_connection(self) -> None:
        client, _conn = self.tcp()
        channel, host_end = self.pair()
        host_end.settimeout(5)
        table = HandoverTable()
        own = pidfd_of(self)
        out = {}
        big = b"x" * (1 << 20)

        def sender() -> None:
            try:
                out["result"] = table.hand_over("host-1", channel, client, big, host_pidfd=own, send_timeout_s=5.0)
            except Refused as exc:
                out["refused"] = exc.detail
            except OSError as exc:
                out["error"] = repr(exc)

        thread = threading.Thread(target=sender)
        thread.start()
        _, got = receive_handover(host_end, 65536, timeout=2.0)  # the first part, with the descriptor
        self.assertIsNotNone(got)
        self.addCleanup(got.close)
        host_end.shutdown(socket.SHUT_RD)  # the host stops reading its channel
        thread.join(8)
        self.assertEqual("channel_broken", out.get("refused"), out)
        self.assertEqual((), table.handed("host-1"))
        self.assertEqual(b"", got.recv(16))
        with self.assertRaises((BrokenPipeError, ConnectionResetError)):
            got.send(b"x")


if __name__ == "__main__":
    unittest.main()
