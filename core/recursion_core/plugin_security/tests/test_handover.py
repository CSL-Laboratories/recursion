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
import shutil
import socket
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
                HandoverTable().hand_over("host-1", a, c, PAYLOAD, host_pid=os.getpid())
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
        return self.table.hand_over("host-1", self.channel, sock, PAYLOAD, host_pid=os.getpid(), **kw)

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
            self.table.hand_over("host-1", half, client, PAYLOAD, host_pid=os.getpid(), send_timeout_s=0.1)
        self.assertEqual((REFUSED_CODE, "channel_broken"), (caught.exception.code, caught.exception.detail))
        self.assertEqual(-1, client.fileno())
        self.assertEqual((), self.table.handed("host-1"))

    def test_the_callers_mistakes_raise_and_close_nothing(self) -> None:
        x, y = socket.socketpair()
        self.addCleanup(x.close)
        self.addCleanup(y.close)
        cases = [dict(host_id=""), dict(payload=b""), dict(payload="text"), dict(host_pid=0),
                 dict(host_pid=True), dict(send_timeout_s=-1), dict(send_timeout_s="1")]
        for case in cases:
            args = dict(host_id="host-1", channel=self.channel, sock=x, payload=PAYLOAD, host_pid=os.getpid())
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
        child.wait(30)
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        x, y = socket.socketpair()
        self.addCleanup(y.close)
        with self.assertRaises(Refused) as caught:
            HandoverTable().hand_over("host-1", a, x, PAYLOAD, host_pid=child.pid)
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
                HandoverTable().hand_over("host-1", a, x, PAYLOAD, host_pid=os.getpid())
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
        result = table.hand_over("host-x", host.channel, client, PAYLOAD, host_pid=host.pid)
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
            table.hand_over("host-x", host.channel, client, PAYLOAD, host_pid=host.pid)
        self.assertEqual((REFUSED_CODE, "other_user_namespace"), (caught.exception.code, caught.exception.detail))
        self.assertEqual(-1, client.fileno())
        self.assertEqual((), table.handed("host-x"))
        host.channel.shutdown(socket.SHUT_WR)
        self.assertEqual({"got": None}, read_line(host.channel))


if __name__ == "__main__":
    unittest.main()
